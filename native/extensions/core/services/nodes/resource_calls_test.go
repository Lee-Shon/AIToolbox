package nodes

import (
 "context"
 "encoding/json"
 "errors"
 "testing"
 "time"

 "github.com/google/uuid"

 "gorm.io/driver/sqlite"
 "gorm.io/gorm"
)

func nativeCallFixture(t *testing.T)(*NodeRegistry,*NativeAllocation){
 t.Helper()
 db,err:=gorm.Open(sqlite.Open(":memory:"),&gorm.Config{});if err!=nil{t.Fatal(err)}
 if err:=db.AutoMigrate(&BackendNode{},&NodeModel{},&NativeAllocation{},&NativeCallReservation{},&NativeResourceWait{});err!=nil{t.Fatal(err)}
 n:=resourceTestNode();if err:=db.Create(&n).Error;err!=nil{t.Fatal(err)}
 policy,_:=json.Marshal(resourceTestPolicy());t.Setenv("LOCALAI_RESOURCE_POLICY",string(policy))
 r:=&NodeRegistry{db:db}
 p:=resourceTestProfile();p.Slots=1
 a,err:=r.ReserveNativeResources(context.Background(),n.ID,"model","rev",p,resourceTestPolicy());if err!=nil{t.Fatal(err)}
 if err:=r.NativeAllocationInstalled(context.Background(),a.ID,"worker:50052");err!=nil{t.Fatal(err)}
 if err:=r.NativeAllocationLoaded(context.Background(),a.ID,"worker:50052");err!=nil{t.Fatal(err)}
 if err:=db.Model(&n).Updates(map[string]any{"native_observation_sequence":4,"native_vram_observation_sequence":4}).Error;err!=nil{t.Fatal(err)}
 if err:=db.Create(&NodeModel{ID:uuid.NewString(),NodeID:n.ID,ModelName:"model",ReplicaIndex:0,Address:"worker:50052",State:"loaded",ConfigRevision:"rev"}).Error;err!=nil{t.Fatal(err)}
 return r,a
}

func TestNativeCallSlotsRemainHeldAfterTransportCancellation(t *testing.T){
 r,a:=nativeCallFixture(t);ctx:=context.Background()
 one,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);if err!=nil{t.Fatal(err)}
 if _,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);!errors.Is(err,ErrResourceWaiting){t.Fatal("backend slot capacity exceeded",err)}
 if err:=r.FinishNativeCall(ctx,one,context.Canceled);err!=nil{t.Fatal(err)}
 if _,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);!errors.Is(err,ErrResourceWaiting){t.Fatal("client cancellation falsely released native slot",err)}
 if err:=r.ReleaseNativeResources(ctx,a.ID,"worker:50052","matching process exited",true);err!=nil{t.Fatal(err)}
 var call NativeCallReservation
 if err:=r.db.First(&call,"id = ?",one).Error;err!=nil{t.Fatal(err)}
 if call.State!="stopped"{t.Fatal("exact stop did not settle held native claim")}
}

func TestNativeCallCompletedSlotReusableButReplacementIdentityRejected(t *testing.T){
 r,a:=nativeCallFixture(t);ctx:=context.Background()
 one,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);if err!=nil{t.Fatal(err)}
 if err:=r.FinishNativeCall(ctx,one,nil);err!=nil{t.Fatal(err)}
 if _,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,"old-generation");!errors.Is(err,ErrResourceObservationUnknown){t.Fatal("stale cached client used a replacement allocation",err)}
 if _,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);err!=nil{t.Fatal(err)}
}

func TestNativeWaitImpossibleHeadDoesNotBlockAndFeasibleOlderDrains(t *testing.T){
 r,a:=nativeCallFixture(t);ctx:=context.Background()
 demand:=resourceTestProfile();demand.PeakVRAM=100
 profile,_:=json.Marshal(demand)
 w:=NativeResourceWait{ModelName:"older",ProfileJSON:string(profile),CandidateIDsJSON:"null",CreatedAt:time.Now().Add(-time.Minute)}
 if err:=r.db.Create(&w).Error;err!=nil{t.Fatal(err)}
 id,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);if err!=nil{t.Fatal("impossible head blocked feasible warm call",err)}
 if err:=r.FinishNativeCall(ctx,id,nil);err!=nil{t.Fatal(err)}
 demand.PeakVRAM=24;profile,_=json.Marshal(demand)
 if err:=r.db.Model(&w).Update("profile_json",string(profile)).Error;err!=nil{t.Fatal(err)}
 if _,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);!errors.Is(err,ErrResourceWaiting){t.Fatal("warm traffic starved older feasible model",err)}
 // A pinned allocation cannot be promised as future free capacity.
 if err:=r.db.Model(&NativeAllocation{}).Where("id = ?",a.ID).Update("eviction_protected",true).Error;err!=nil{t.Fatal(err)}
 if _,err:=r.AcquireNativeCall(ctx,a.NodeID,a.ModelName,0,a.ID);err!=nil{t.Fatal("unreclaimable pinned allocation blocked useful work",err)}
}
