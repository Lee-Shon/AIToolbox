package nodes

import (
 "context"
 "encoding/json"
 "errors"
 "fmt"
 "time"

 "github.com/google/uuid"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "gorm.io/gorm"
 "gorm.io/gorm/clause"
)

// Routing reservations and actual inference slots have different lifetimes.
// The native in-flight counter retains its eviction role; these durable claims
// enforce measured backend capacity, including requests using cached clients.
type NativeCallReservation struct {
 ID string `gorm:"primaryKey;size:36" json:"id"`
 AllocationID string `gorm:"index;size:36" json:"allocation_id"`
 State string `gorm:"index" json:"state"`
 CreatedAt time.Time `json:"created_at"`
 UpdatedAt time.Time `json:"updated_at"`
}

func (r *NodeRegistry) AcquireNativeCall(ctx context.Context,nodeID,modelName string,index int,allocationID string)(string,error){
 policy,_,err:=nativeResourcePolicy();if err!=nil{return "",err}
 id:=uuid.NewString()
 err=r.db.WithContext(ctx).Transaction(func(tx *gorm.DB)error{
  // Same node-first lock order as cold admission. A simultaneous cold load,
  // cached-client dispatch and heartbeat cannot each spend the same capacity.
  var node BackendNode
  if err:=tx.Clauses(clause.Locking{Strength:"UPDATE"}).First(&node,"id = ?",nodeID).Error;err!=nil{return err}
  var held []NativeAllocation
  if err:=tx.Where("node_id = ? AND state <> ?",nodeID,"released").Find(&held).Error;err!=nil{return err}
  var a *NativeAllocation
  for i:=range held {if held[i].ModelName==modelName&&held[i].ReplicaIndex==index&&held[i].State=="live"&&held[i].ID==allocationID{a=&held[i];break}}
  if a==nil{return ErrResourceObservationUnknown}
  var replica NodeModel
  if err:=tx.Clauses(clause.Locking{Strength:"UPDATE"}).Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state = ? AND config_revision = ?",
   nodeID,modelName,index,a.Address,"loaded",a.ConfigRevision).First(&replica).Error;err!=nil{return err}
  if err:=nativeFit(node,held,NativeResourceProfile{},policy,time.Now());err!=nil{return err}
  older,err:=nativeOlderWait(tx,node,held,modelName,policy);if err!=nil{return err}
  if older!=nil {
   var demand NativeResourceProfile
   if err:=json.Unmarshal([]byte(older.ProfileJSON),&demand);err!=nil{return err}
   if nativeFit(node,held,demand,policy,time.Now())!=nil{return fmt.Errorf("draining for older feasible model %s: %w",older.ModelName,ErrResourceWaiting)}
  }
  var p NativeResourceProfile
  if err:=json.Unmarshal([]byte(a.ProfileJSON),&p);err!=nil{return err}
  if p.Slots<1{return ErrResourceProfileRequired}
  var active int64
  if err:=tx.Model(&NativeCallReservation{}).Where("allocation_id = ? AND state IN ?",a.ID,[]string{"active","unknown"}).Count(&active).Error;err!=nil{return err}
  if active>=int64(p.Slots){return fmt.Errorf("measured native inference slots occupied: %w",ErrResourceWaiting)}
  return tx.Create(&NativeCallReservation{ID:id,AllocationID:a.ID,State:"active"}).Error
 })
 return id,err
}

func (r *NodeRegistry) FinishNativeCall(ctx context.Context,id string,callErr error)error{
 state:="finished"
 // Transport cancellation acknowledges the client connection, not that GPU
 // execution stopped. Hold the claim until a native execution/stop receipt.
 if errors.Is(callErr,context.Canceled)||errors.Is(callErr,context.DeadlineExceeded)||errors.Is(callErr,ErrResourceStopUnconfirmed){state="unknown"}
 switch status.Code(callErr){case codes.Canceled,codes.DeadlineExceeded,codes.Unavailable,codes.Unknown:if callErr!=nil{state="unknown"}}
 return r.db.WithContext(ctx).Model(&NativeCallReservation{}).Where("id = ? AND state = ?",id,"active").Update("state",state).Error
}

func (c *InFlightTrackingClient) nativeTrack(ctx context.Context)(func(error),error){
 if !nativeResourcesEnabled(){return func(error){},nil}
 registry,ok:=c.registry.(interface{
  AcquireNativeCall(context.Context,string,string,int,string)(string,error)
  FinishNativeCall(context.Context,string,error)error
 });if !ok{return nil,fmt.Errorf("native call admission owner unavailable")}
 var id string
 var err error
 for {
  id,err=registry.AcquireNativeCall(ctx,c.nodeID,c.modelName,c.replicaIndex,c.nativeAllocationID)
  if !errors.Is(err,ErrResourceWaiting){break}
  select{case <-ctx.Done():return nil,ctx.Err();case <-time.After(250*time.Millisecond):}
 }
 if err!=nil{return nil,err}
 return func(callErr error){
  finishCtx,cancel:=context.WithTimeout(context.Background(),5*time.Second);defer cancel()
  // If this write fails the durable active claim remains held. Never dispatch
  // a second request based on an in-memory decrement alone.
  _=registry.FinishNativeCall(finishCtx,id,callErr)
 },nil
}
