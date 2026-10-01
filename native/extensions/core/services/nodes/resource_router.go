package nodes

import (
 "context"
 "encoding/json"
 "errors"
 "fmt"
 "os"
 "sort"
 "time"

 "github.com/mudler/LocalAI/core/services/messaging"
 pb "github.com/mudler/LocalAI/pkg/grpc/proto"
 "github.com/mudler/LocalAI/pkg/model"
 "gorm.io/gorm"
 "gorm.io/gorm/clause"
)

// Opt-in is deliberately explicit: a missing policy is not an invented budget.
// Candidate activation supplies this policy only after per-binding calibration.
func nativeResourcePolicy() (NativeResourcePolicy, bool, error) {
 raw, enabled := os.LookupEnv("LOCALAI_RESOURCE_POLICY")
 var p NativeResourcePolicy
 if !enabled { return p, false, nil }
 if err := json.Unmarshal([]byte(raw), &p); err != nil { return p, true, err }
 if p.Revision == "" || p.ObservationMaxAge <= 0 { return p, true, fmt.Errorf("invalid native resource policy") }
 return p, true, nil
}

func nativeResourcesEnabled() bool { _, enabled := os.LookupEnv("LOCALAI_RESOURCE_POLICY"); return enabled }

type nativeReservedInstaller interface {
 InstallReservedBackend(context.Context, *NativeAllocation, string, string) (string, error)
}

// This uses a distinct native subject so an older worker cannot silently ignore
// allocation identity. A timeout leaves the durable reservation in quarantine.
func (a *RemoteUnloaderAdapter) InstallReservedBackend(ctx context.Context, allocation *NativeAllocation, backend, galleries string) (string, error) {
 req := messaging.BackendInstallRequest{Backend:backend,ModelID:allocation.ModelName,
  ReplicaIndex:int32(allocation.ReplicaIndex),BackendGalleries:galleries,AllocationID:allocation.ID}
 type result struct { reply *messaging.BackendInstallReply; err error }
 done:=make(chan result,1)
 go func(){ reply,err:=messaging.RequestJSON[messaging.BackendInstallRequest,messaging.BackendInstallReply](a.nats,
  messaging.SubjectNodeBackendInstall(allocation.NodeID)+".reserved",req,a.installTimeout); done<-result{reply,err} }()
 select {
 case <-ctx.Done(): return "",ctx.Err()
 case res:=<-done:
  if res.err!=nil {return "",res.err}
  if !res.reply.Success || res.reply.Address=="" || res.reply.AllocationID!=allocation.ID {return "",fmt.Errorf("reserved backend install unconfirmed: %s",res.reply.Error)}
  return res.reply.Address,nil
 }
}

// Selection and allocation are LocalAI-owned. Failed/stale observations never
// fall back to the legacy best-effort scheduler or to an unreserved install.
func (r *SmartRouter) scheduleNativeReserved(ctx context.Context, backend, modelName string, opts *pb.ModelOptions) (*BackendNode,string,int,error) {
 policy,_,err:=nativeResourcePolicy(); if err!=nil{return nil,"",0,err}
 profile,err:=nativeProfile(opts);if err!=nil{return nil,"",0,err}
 if r.db==nil{return nil,"",0,fmt.Errorf("native resource database unavailable")}
 installer,ok:=r.unloader.(nativeReservedInstaller);if !ok{return nil,"",0,fmt.Errorf("reserved native installer unavailable")}
 sched,err:=r.registry.GetGoverningScheduling(ctx,modelName);if err!=nil&&!errors.Is(err,gorm.ErrRecordNotFound){return nil,"",0,err}
 ids,err:=r.resolveSelectorCandidates(ctx,modelName,sched);if err!=nil{return nil,"",0,err}
 ids,err=r.narrowByDiskHeadroom(ctx,modelName,opts,ids);if err!=nil{return nil,"",0,err}
 registry:=&NodeRegistry{db:r.db}
 candidates,err:=registry.List(ctx);if err!=nil{return nil,"",0,err}
 sort.SliceStable(candidates,func(i,j int)bool{return candidates[i].AvailableVRAM>candidates[j].AvailableVRAM})
 revision,err:=r.registry.GetModelConfigRevision(ctx,modelName);if err!=nil{return nil,"",0,err}
 var last error=ErrResourceWaiting
 for i:=range candidates {
  n:=&candidates[i]
  if ids!=nil {allowed:=false;for _,id:=range ids{if id==n.ID{allowed=true;break}};if !allowed{continue}}
  if err:=r.unloader.PingNode(n.ID);err!=nil{last=err;continue}
  protected:=sched!=nil&&sched.MinReplicas>0
  for _,name:=range r.pinnedModelNames(){if name==modelName{protected=true}}
  allocation,err:=registry.ReserveNativeResources(ctx,n.ID,modelName,revision,profile,policy,protected)
  if err!=nil{last=err;continue}
  address,err:=installer.InstallReservedBackend(ctx,allocation,backend,r.galleriesJSON)
  if err!=nil {
   // Dispatch may have reached the worker. Never turn an uncertain install
   // into free capacity or a second loader for the same logical slot.
   r.db.WithContext(context.WithoutCancel(ctx)).Model(&NativeAllocation{}).Where("id = ?",allocation.ID).Update("state","quarantine")
   return nil,"",0,fmt.Errorf("native install quarantined (%s): %w",allocation.ID,err)
  }
  if err:=registry.NativeAllocationInstalled(ctx,allocation.ID,address);err!=nil{return nil,"",0,err}
  return n,address,allocation.ReplicaIndex,nil
 }
 return nil,"",0,last
}

func (r *SmartRouter) publishNativeLoaded(ctx context.Context,nodeID,modelName string,index int,address string) error {
 var a NativeAllocation
 if err:=r.db.WithContext(ctx).Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state = ?",
 nodeID,modelName,index,address,"reserved").First(&a).Error;err!=nil{return err}
 return (&NodeRegistry{db:r.db}).NativeAllocationLoaded(ctx,a.ID,address)
}

// Quarantine is a scheduling fence, not a physical stop. The existing cleanup
// service obtains the worker receipt and only then releases the allocation.
func (r *SmartRouter) quarantineNativeReplica(ctx context.Context,nodeID,modelName string,index int,address string) error {
 if r.db==nil{return ErrResourceStopUnconfirmed}
 var replica NodeModel
 var allocationID string
 err:=r.db.WithContext(ctx).Transaction(func(tx *gorm.DB)error{
  if err:=tx.Clauses(clause.Locking{Strength:"UPDATE"}).Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ?",
    nodeID,modelName,index,address).First(&replica).Error;err!=nil{return err}
  if replica.InFlight!=0{return ErrEvictionBusy}
  if replica.State!="loaded"{return ErrResourceStopUnconfirmed}
  var active int64
  if err:=tx.Model(&NativeCallReservation{}).Where("allocation_id IN (SELECT id FROM native_allocations WHERE node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state <> ?) AND state IN ?",
    nodeID,modelName,index,address,"released",[]string{"active","unknown"}).Count(&active).Error;err!=nil{return err}
  if active!=0{return ErrEvictionBusy}
  var allocation NativeAllocation
  if err:=tx.Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND config_revision = ? AND state <> ?",
   nodeID,modelName,index,address,replica.ConfigRevision,"released").First(&allocation).Error;err!=nil{return err}
  allocationID=allocation.ID
  replica.State="unloading"
  // The immediate caller owns the same lease as the background cleanup loop.
  // Making the row due immediately lets both send an exact stop; one then
  // mistakes the other's successful row removal for an unconfirmed stop.
  if err:=tx.Model(&replica).Updates(map[string]any{"state":"unloading","cleanup_next_retry_at":time.Now().Add(modelCleanupLease)}).Error;err!=nil{return err}
  return tx.Model(&NativeAllocation{}).Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state <> ?",
    nodeID,modelName,index,address,"released").Update("state","quarantine").Error
 });if err!=nil{return err}
 if r.modelCleanup==nil{return ErrResourceStopUnconfirmed}
 if r.modelCleanup.Cleanup(context.WithoutCancel(ctx),[]NodeModel{replica},false)!=0{
  // A health/reconciliation observer can also remove the old registry row.
  // Only its exact durable physical-stop receipt can make that idempotent.
  var confirmed int64
  query:=r.db.WithContext(ctx).Model(&NativeAllocation{}).Where("id = ? AND state = ? AND stop_evidence <> ?",allocationID,"released","")
  if err:=query.Count(&confirmed).Error;err!=nil{return err}
  var remaining int64
  if err:=r.db.WithContext(ctx).Model(&NodeModel{}).Where("id = ?",replica.ID).Count(&remaining).Error;err!=nil{return err}
  if confirmed==0||remaining!=0{return ErrResourceStopUnconfirmed}
 }
 return nil
}

func (r *SmartRouter) unloadNativeReserved(ctx context.Context,nodeID,modelName string) error {
 var replicas []NodeModel
 if err:=r.db.WithContext(ctx).Where("node_id = ? AND model_name = ?",nodeID,modelName).Find(&replicas).Error;err!=nil{return err}
 for _,replica:=range replicas {
  if err:=r.quarantineNativeReplica(ctx,nodeID,modelName,replica.ReplicaIndex,replica.Address);err!=nil{return err}
 }
 return nil
}

func (r *NodeRegistry) ConfirmNativeStop(ctx context.Context,replica NodeModel,reply messaging.ModelStopReply) error {
 var a NativeAllocation
 err:=r.db.WithContext(ctx).Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state <> ?",
 replica.NodeID,replica.ModelName,replica.ReplicaIndex,replica.Address,"released").First(&a).Error
 if errors.Is(err,gorm.ErrRecordNotFound){return nil};if err!=nil{return err}
 if !reply.Terminated || reply.AllocationID!=a.ID || reply.ProcessKey!=model.BackendProcessKey(replica.ModelName,replica.ReplicaIndex) {return ErrResourceStopUnconfirmed}
 if reply.Matched && reply.Address!=replica.Address{return ErrResourceStopUnconfirmed}
 b,err:=json.Marshal(reply);if err!=nil{return err}
 return r.ReleaseNativeResources(ctx,a.ID,replica.Address,string(b),true)
}

func (r *NodeRegistry) NativeStopAllocationID(ctx context.Context,replica NodeModel) (string,error) {
 var a NativeAllocation
 err:=r.db.WithContext(ctx).Where("node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state <> ?",
 replica.NodeID,replica.ModelName,replica.ReplicaIndex,replica.Address,"released").First(&a).Error
 if errors.Is(err,gorm.ErrRecordNotFound){return "",nil};return a.ID,err
}
