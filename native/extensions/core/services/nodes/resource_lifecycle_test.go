package nodes

import (
 "context"
 "errors"
 "fmt"
 "testing"
 "time"

 "github.com/mudler/LocalAI/core/services/messaging"
 "github.com/mudler/LocalAI/pkg/model"
)

type nativeReceiptStopper struct{ allocationID string; calls int }
func (s *nativeReceiptStopper) StopModelReplica(_ context.Context,_ string,row NodeModel,_ bool)(messaging.ModelStopReply,error){
 s.calls++
 return messaging.ModelStopReply{AllocationID:s.allocationID,ProcessKey:model.BackendProcessKey(row.ModelName,row.ReplicaIndex),Address:row.Address,Terminated:true},nil
}

type nativeOverlappingCleanupStopper struct{ registry *NodeRegistry; allocationID string; t *testing.T; removeObservedRow bool }
func(s *nativeOverlappingCleanupStopper)StopModelReplica(ctx context.Context,_ string,row NodeModel,_ bool)(messaging.ModelStopReply,error){
 // Drive the real background selector while the administrative stop owns it.
 rows,err:=s.registry.ClaimModelCleanupRetries(ctx,time.Now().Add(time.Second),time.Now().Add(modelCleanupLease),10)
 if err!=nil||len(rows)!=0{s.t.Fatalf("background cleanup stole immediate stop lease: %v %v",rows,err)}
 if s.removeObservedRow{
  if err:=s.registry.db.WithContext(ctx).Where("id = ?",row.ID).Delete(&NodeModel{}).Error;err!=nil{s.t.Fatal(err)}
 }
 return messaging.ModelStopReply{AllocationID:s.allocationID,ProcessKey:model.BackendProcessKey(row.ModelName,row.ReplicaIndex),Address:row.Address,Terminated:true},nil
}

func TestNativeAdminStopOwnsLeaseAndAcceptsOnlyDurableConfirmedConcurrentRemoval(t *testing.T){
 for _,removed:=range []bool{false,true}{
  t.Run(fmt.Sprint(removed),func(t *testing.T){
   r,a:=nativeCallFixture(t)
   stopper:=&nativeOverlappingCleanupStopper{registry:r,allocationID:a.ID,t:t,removeObservedRow:removed}
   if err:=r.NativeUnloadIdle(context.Background(),a.NodeID,a.ModelName,stopper);err!=nil{t.Fatal(err)}
   if err:=r.db.First(a,"id = ?",a.ID).Error;err!=nil{t.Fatal(err)}
   if a.State!="released"||a.StopEvidence==""{t.Fatal("physical stop proof missing")}
  })
 }
}

func TestNativeAdminUnloadCannotKillActivePeerAndReleasesOnlyAfterReceipt(t *testing.T){
 r,a:=nativeCallFixture(t);ctx:=context.Background();stopper:=&nativeReceiptStopper{allocationID:a.ID}
 call,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);if err!=nil{t.Fatal(err)}
 if err=r.NativeUnloadIdle(ctx,a.NodeID,a.ModelName,stopper);!errors.Is(err,ErrEvictionBusy){t.Fatal("administrative unload bypassed active inference",err)}
 if stopper.calls!=0{t.Fatal("active request was sent a stop command")}
 if err=r.FinishNativeCall(ctx,call,nil);err!=nil{t.Fatal(err)}
 if err=r.NativeUnloadIdle(ctx,a.NodeID,a.ModelName,stopper);err!=nil{t.Fatal(err)}
 if stopper.calls!=1{t.Fatal("missing exact stop")}
 if err=r.db.First(a,"id = ?",a.ID).Error;err!=nil{t.Fatal(err)}
 if a.State!="released"||a.StopEvidence==""{t.Fatal("stop did not settle durable allocation")}
 var remaining int64
 if err=r.db.Model(&NodeModel{}).Where("node_id = ? AND model_name = ?",a.NodeID,a.ModelName).Count(&remaining).Error;err!=nil||remaining!=0{t.Fatal("confirmed original registry row remained",err)}
}

func TestNativeAdminUnloadUnknownOrOrphanIsNotSuccessful(t *testing.T){
 r,a:=nativeCallFixture(t);ctx:=context.Background();stopper:=&nativeReceiptStopper{allocationID:a.ID}
 call,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);if err!=nil{t.Fatal(err)}
 if err=r.FinishNativeCall(ctx,call,context.Canceled);err!=nil{t.Fatal(err)}
 if err=r.NativeUnloadIdle(ctx,a.NodeID,a.ModelName,stopper);!errors.Is(err,ErrEvictionBusy){t.Fatal("unconfirmed call ignored",err)}
 if err=r.db.Where("node_id = ?",a.NodeID).Delete(&NodeModel{}).Error;err!=nil{t.Fatal(err)}
 if err=r.NativeUnloadIdle(ctx,a.NodeID,a.ModelName,stopper);!errors.Is(err,ErrResourceStopUnconfirmed){t.Fatal("registry disappearance became a stop receipt",err)}
 if stopper.calls!=0{t.Fatal("unknown peer was killed")}
}
