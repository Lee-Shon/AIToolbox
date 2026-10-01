package nodes

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"
	"time"

	pb "github.com/mudler/LocalAI/pkg/grpc/proto"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

func resourceTestProfile() NativeResourceProfile {
	return NativeResourceProfile{Revision:"measured-shape", EvidenceSHA256:strings.Repeat("a",64),
		ResidentFloorVRAM:4, PeakVRAM:6, ResidentFloorRAM:3, PeakRAM:5, Slots:2, Batch:1}
}

func resourceTestNode() BackendNode {
	now:=time.Now()
	return BackendNode{ID:"gpu",Name:"gpu",Status:StatusHealthy,TotalRAM:100,AvailableRAM:100,
		TotalVRAM:30,AvailableVRAM:30,LastHeartbeat:now,MaxReplicasPerModel:1,NativeObservationSequence:2,NativeVRAMObservationSequence:2,NativeRAMObservedAt:&now,NativeVRAMObservedAt:&now}
}

func resourceTestPolicy() NativeResourcePolicy {
	return NativeResourcePolicy{Revision:"policy-1",RAMBudget:80,RAMHeadroom:5,VRAMHeadroom:2,ObservationMaxAge:time.Minute}
}

func TestNativeResourceFitSeparatesPoolPhysicalAndUnknown(t *testing.T) {
	n,p,policy := resourceTestNode(),resourceTestProfile(),resourceTestPolicy()
	if err:=nativeFit(n,nil,p,policy,time.Now());err!=nil {t.Fatal(err)}
	n.VRAMBudgetBytes=4
	if !errors.Is(nativeFit(n,nil,p,policy,time.Now()),ErrResourceWaiting) {t.Fatal("budget bypass")}
	n.VRAMBudgetBytes=0;n.AvailableVRAM=0
	if !errors.Is(nativeFit(n,nil,p,policy,time.Now()),ErrResourceWaiting) {t.Fatal("zero free treated as unknown/available")}
	n.AvailableVRAM=30;n.TotalVRAM=0
	if !errors.Is(nativeFit(n,nil,p,policy,time.Now()),ErrResourceObservationUnknown) {t.Fatal("missing total became zero demand")}
	n=resourceTestNode();n.LastHeartbeat=time.Now().Add(-2*time.Minute)
	if !errors.Is(nativeFit(n,nil,p,policy,time.Now()),ErrResourceObservationUnknown) {t.Fatal("stale observation admitted")}
	n=resourceTestNode();n.AvailableRAM=1
	if !errors.Is(nativeFit(n,nil,p,policy,time.Now()),ErrResourceWaiting) {t.Fatal("RAM pressure ignored")}
}

func TestNativeResourcePendingLoadsSurviveHeartbeatAndWeightsCountOnce(t *testing.T) {
	n,p,policy:=resourceTestNode(),resourceTestProfile(),resourceTestPolicy()
	n.AvailableVRAM=10
	a:=NativeAllocation{State:"reserved",PeakVRAM:6,PeakRAM:5,ResidentFloorVRAM:4,ResidentFloorRAM:3}
	if !errors.Is(nativeFit(n,[]NativeAllocation{a},p,policy,time.Now()),ErrResourceWaiting) {t.Fatal("incomplete load was erased by heartbeat")}
	a.State="live"
	n.NativeVRAMObservationSequence=1
	if !errors.Is(nativeFit(n,[]NativeAllocation{a},p,policy,time.Now()),ErrResourceWaiting) {t.Fatal("pre-load heartbeat discounted resident weights")}
	n.NativeVRAMObservationSequence=2
	if err:=nativeFit(n,[]NativeAllocation{a},p,policy,time.Now());err!=nil {t.Fatalf("resident weights counted twice: %v",err)}
	a.State="quarantine"
	if !errors.Is(nativeFit(n,[]NativeAllocation{a},p,policy,time.Now()),ErrResourceWaiting) {t.Fatal("unconfirmed stop freed memory")}
	a.State="released"
	if err:=nativeFit(n,[]NativeAllocation{a},p,policy,time.Now());err!=nil {t.Fatal(err)}
	a.State="reserved";a.PeakVRAM=math.MaxUint64
	if err:=nativeFit(n,[]NativeAllocation{a},p,policy,time.Now());err==nil {t.Fatal("resource overflow admitted")}
}

func TestNativeResourceProfileInvalidation(t *testing.T) {
	opts:=&pb.ModelOptions{CUDA:true,ContextSize:4096,Options:[]string{"max_inference_batch_size:1"}}
	p:=resourceTestProfile();p.ConfigurationSHA256,_=nativeConfigurationHash(opts)
	b,_:=json.Marshal(p);opts.Options=append(opts.Options,"resource_profile:"+string(b))
	if _,err:=nativeProfile(opts);err!=nil {t.Fatal(err)}
	opts.Seed=731
	if _,err:=nativeProfile(opts);err!=nil {t.Fatal("native random seed invalidated an unchanged resource shape",err)}
	opts.ContextSize++
	if _,err:=nativeProfile(opts);err==nil {t.Fatal("old shape evidence reused after configuration edit")}
	if _,err:=nativeProfile(&pb.ModelOptions{CUDA:true});!errors.Is(err,ErrResourceProfileRequired) {t.Fatal("unknown profile admitted")}
}

func TestNativeResourceReleaseRequiresExactStopAndKeepsOtherAllocations(t *testing.T) {
	db,err:=gorm.Open(sqlite.Open(":memory:"),&gorm.Config{})
	if err!=nil {t.Fatal(err)}
	if err=db.AutoMigrate(&BackendNode{},&NodeModel{},&NativeAllocation{},&NativeCallReservation{});err!=nil {t.Fatal(err)}
	n:=resourceTestNode();if err=db.Create(&n).Error;err!=nil {t.Fatal(err)}
	r:=&NodeRegistry{db:db};ctx:=context.Background()
	a,err:=r.ReserveNativeResources(ctx,n.ID,"model-a","revision-a",resourceTestProfile(),resourceTestPolicy());if err!=nil {t.Fatal(err)}
	b,err:=r.ReserveNativeResources(ctx,n.ID,"model-b","revision-b",resourceTestProfile(),resourceTestPolicy());if err!=nil {t.Fatal(err)}
	if err=r.NativeAllocationInstalled(ctx,a.ID,"worker:50052");err!=nil {t.Fatal(err)}
	if err=r.NativeAllocationLoaded(ctx,a.ID,"worker:50052");err!=nil {t.Fatal(err)}
	if err=r.ReleaseNativeResources(ctx,a.ID,"worker:50052","cancel requested",false);!errors.Is(err,ErrResourceStopUnconfirmed){t.Fatal(err)}
	if err=r.ReleaseNativeResources(ctx,a.ID,"worker:50099","other process stop",true);err==nil{t.Fatal("foreign receipt released resources")}
	if err=r.ReleaseNativeResources(ctx,a.ID,"worker:50052","exact native stop proof",true);err!=nil{t.Fatal(err)}
	if err=r.ReleaseNativeResources(ctx,a.ID,"worker:50052","same proof",true);err!=nil{t.Fatal(err)}
	var after NativeAllocation
	if err=db.First(&after,"id = ?",b.ID).Error;err!=nil{t.Fatal(err)}
	if after.State!="reserved"||after.ReleasedAt!=nil{t.Fatal("unrelated request allocation was released")}
}
