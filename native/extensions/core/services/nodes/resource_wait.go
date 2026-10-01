package nodes

import (
 "context"
 "encoding/json"
 "errors"
 "time"

 pb "github.com/mudler/LocalAI/pkg/grpc/proto"
 "gorm.io/gorm"
 "gorm.io/gorm/clause"
)

// A durable native cold-load waiter preserves arrival order across frontend
// replicas. It is refreshed by the existing model-load job, not by new callers
// of a popular model. Repeated warm traffic cannot reset the waiting age.
type NativeResourceWait struct {
 ModelName string `gorm:"primaryKey;size:255" json:"model_name"`
 ConfigRevision string `json:"config_revision"`
 ProfileJSON string `gorm:"type:text" json:"profile_json"`
 CandidateIDsJSON string `gorm:"type:text" json:"candidate_node_ids"`
 CreatedAt time.Time `json:"created_at"`
 UpdatedAt time.Time `json:"updated_at"`
}

func nativeWaitAllowed(w NativeResourceWait,nodeID string)bool{
 var ids []string
 if json.Unmarshal([]byte(w.CandidateIDsJSON),&ids)!=nil{return false}
 if ids==nil{return true};for _,id:=range ids{if id==nodeID{return true}};return false
}

// Determine whether the older request could run after the CURRENT work drains.
// Unmanaged/external memory never becomes hypothetically free. Quarantined
// generations remain charged because their stop is not confirmed.
func nativeFitAfterDrain(node BackendNode,held []NativeAllocation,p NativeResourceProfile,policy NativeResourcePolicy)bool{
 remaining:=make([]NativeAllocation,0,len(held))
 for _,a:=range held{
  if a.State!="live"||a.EvictionProtected{remaining=append(remaining,a);continue}
  var err error
  if node.NativeVRAMObservationSequence>a.LoadVRAMObservationSequence && node.NativeVRAMObservationSequence-a.LoadVRAMObservationSequence>=2 {
   node.AvailableVRAM,err=addResource(node.AvailableVRAM,a.ResidentFloorVRAM);if err!=nil{return false}
  }
  if node.NativeObservationSequence>a.LoadObservationSequence && node.NativeObservationSequence-a.LoadObservationSequence>=2 {
   node.AvailableRAM,err=addResource(node.AvailableRAM,a.ResidentFloorRAM);if err!=nil{return false}
  }
 }
 node.AvailableVRAM=min(node.AvailableVRAM,node.TotalVRAM)
 node.AvailableRAM=min(node.AvailableRAM,node.TotalRAM)
 return nativeFit(node,remaining,p,policy,time.Now())==nil
}

func nativeOlderWait(tx *gorm.DB,node BackendNode,held []NativeAllocation,modelName string,policy NativeResourcePolicy)(*NativeResourceWait,error){
 var waits []NativeResourceWait
 // This is an ownership lease only. Expiry never frees a physical allocation.
 if err:=tx.Where("updated_at >= ?",time.Now().Add(-2*policy.ObservationMaxAge)).Order("created_at ASC, model_name ASC").Find(&waits).Error;err!=nil{return nil,err}
 for i:=range waits{
  w:=&waits[i]
  if w.ModelName==modelName{return nil,nil}
  if !nativeWaitAllowed(*w,node.ID){continue}
  var p NativeResourceProfile
  if json.Unmarshal([]byte(w.ProfileJSON),&p)!=nil{continue}
  if nativeFitAfterDrain(node,held,p,policy){return w,nil}
 }
 return nil,nil
}

func (r *SmartRouter) waitNativeReserved(ctx context.Context,backend,modelName string,opts *pb.ModelOptions)(*BackendNode,string,int,error){
 p,err:=nativeProfile(opts);if err!=nil{return nil,"",0,err}
 policy,_,err:=nativeResourcePolicy();if err!=nil{return nil,"",0,err}
 sched,err:=r.registry.GetGoverningScheduling(ctx,modelName);if err!=nil&&!errors.Is(err,gorm.ErrRecordNotFound){return nil,"",0,err}
 ids,err:=r.resolveSelectorCandidates(ctx,modelName,sched);if err!=nil{return nil,"",0,err}
 revision,err:=r.registry.GetModelConfigRevision(ctx,modelName);if err!=nil{return nil,"",0,err}
 profileJSON,err:=json.Marshal(p);if err!=nil{return nil,"",0,err}
 idsJSON,err:=json.Marshal(ids);if err!=nil{return nil,"",0,err}
 waiter:=NativeResourceWait{ModelName:modelName,ConfigRevision:revision,ProfileJSON:string(profileJSON),CandidateIDsJSON:string(idsJSON)}
 defer r.db.WithContext(context.WithoutCancel(ctx)).Where("model_name = ? AND config_revision = ?",modelName,revision).Delete(&NativeResourceWait{})
 for {
  waiter.UpdatedAt=time.Now()
  if err:=r.db.WithContext(ctx).Clauses(clause.OnConflict{Columns:[]clause.Column{{Name:"model_name"}},DoUpdates:clause.AssignmentColumns([]string{"updated_at"})}).Create(&waiter).Error;err!=nil{return nil,"",0,err}
  node,address,index,err:=r.scheduleNativeReserved(ctx,backend,modelName,opts)
  if err==nil{return node,address,index,nil}
  if !errors.Is(err,ErrResourceWaiting)&&!errors.Is(err,ErrNoFreeSlot)&&!errors.Is(err,ErrResourceObservationUnknown){return nil,"",0,err}
  // Eviction is attempted only for this waiter's eligible nodes, after all
  // fitting nodes were considered. The claim and exact stop protect peers.
  _=r.drainNativeForWaiter(ctx,waiter,policy)
  reportLoadPhase(ctx,"waiting_resources",nil,0)
  select{case <-ctx.Done():return nil,"",0,ctx.Err();case <-time.After(time.Second):}
 }
}

func (r *SmartRouter) drainNativeForWaiter(ctx context.Context,w NativeResourceWait,policy NativeResourcePolicy)error{
 var nodes []BackendNode
 if err:=r.db.WithContext(ctx).Where("status = ?",StatusHealthy).Find(&nodes).Error;err!=nil{return err}
 var demand NativeResourceProfile;if err:=json.Unmarshal([]byte(w.ProfileJSON),&demand);err!=nil{return err}
 for _,node:=range nodes{
  if !nativeWaitAllowed(w,node.ID){continue}
  var held []NativeAllocation
  if err:=r.db.WithContext(ctx).Where("node_id = ? AND state <> ?",node.ID,"released").Find(&held).Error;err!=nil{return err}
  if !nativeFitAfterDrain(node,held,demand,policy){continue}
  older,err:=nativeOlderWait(r.db.WithContext(ctx),node,held,w.ModelName,policy);if err!=nil{return err};if older!=nil{continue}
  q:=r.db.WithContext(ctx).Where("node_id = ? AND model_name <> ? AND state = ? AND in_flight = 0",node.ID,w.ModelName,"loaded")
  if pinned:=r.pinnedModelNames();len(pinned)>0{q=q.Where("model_name NOT IN ?",pinned)}
  // Keep native minimum-replica policies. A binding promising a resident
  // minimum cannot be evicted behind that policy owner's back.
  q=q.Where("NOT EXISTS (SELECT 1 FROM model_scheduling_configs sc WHERE COALESCE(NULLIF(sc.target_model, ''), sc.model_name) = node_models.model_name AND sc.min_replicas > 0)")
  var replicas []NodeModel
  if err:=q.Order("last_used ASC").Find(&replicas).Error;err!=nil{return err}
  for _,replica:=range replicas{
   if err:=r.quarantineNativeReplica(ctx,replica.NodeID,replica.ModelName,replica.ReplicaIndex,replica.Address);err==nil{return nil}
  }
 }
 return ErrResourceWaiting
}
