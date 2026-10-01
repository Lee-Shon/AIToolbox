package nodes

import (
 "context"
 "fmt"
)

// Every native administrative unload must use the same durable claims and
// exact process receipt as resource-driven eviction. Legacy Free()/stop-all
// commands cannot bypass the policy or clear a replacement registry row.
func (a *RemoteUnloaderAdapter) nativeUnloadIdle(ctx context.Context,nodeID,target string)error{
 registry,ok:=a.registry.(*NodeRegistry)
 if !ok{return fmt.Errorf("native resource lifecycle owner unavailable")}
 return registry.NativeUnloadIdle(ctx,nodeID,target,a)
}

func (r *NodeRegistry) NativeUnloadIdle(ctx context.Context,nodeID,target string,stopper ExactModelStopper)error{
 var replicas []NodeModel
 q:=r.db.WithContext(ctx).Where("node_id = ?",nodeID)
 if target!="" {q=q.Where("model_name = ? OR backend_type = ?",target,target)}
 if err:=q.Find(&replicas).Error;err!=nil{return err}
 // Preflight the whole matching set before touching any process. The per-row
 // transactional fence below rechecks this against concurrent dispatch.
 for _,replica:=range replicas {
  if replica.InFlight!=0{return ErrEvictionBusy}
  if err:=r.NativeStopReady(ctx,replica);err!=nil{return err}
 }
 if len(replicas)==0 {
  var held int64
  q:=r.db.WithContext(ctx).Model(&NativeAllocation{}).Where("node_id = ? AND state <> ?",nodeID,"released")
  if target!="" {q=q.Where("model_name = ?",target)}
  if err:=q.Count(&held).Error;err!=nil{return err}
  if held!=0{return ErrResourceStopUnconfirmed}
  return nil
 }
 router:=&SmartRouter{db:r.db,registry:r,modelCleanup:NewModelCleanupService(r,stopper)}
 for _,replica:=range replicas {
  if err:=router.quarantineNativeReplica(ctx,nodeID,replica.ModelName,replica.ReplicaIndex,replica.Address);err!=nil{return err}
 }
 return nil
}

// Config revision cleanup also reaches StopModelReplica directly. A revision
// fence may close admission immediately, but active healthy calls drain before
// the process is stopped. A forced admin flag is not permission to kill peers.
func (r *NodeRegistry) NativeStopReady(ctx context.Context,replica NodeModel)error{
 var active int64
 err:=r.db.WithContext(ctx).Model(&NativeCallReservation{}).Where(
  "allocation_id IN (SELECT id FROM native_allocations WHERE node_id = ? AND model_name = ? AND replica_index = ? AND address = ? AND state <> ?) AND state = ?",
  replica.NodeID,replica.ModelName,replica.ReplicaIndex,replica.Address,"released","active").Count(&active).Error
 if err!=nil{return err};if active!=0{return ErrEvictionBusy};return nil
}
