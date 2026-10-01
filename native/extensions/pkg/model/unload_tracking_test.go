package model

import (
	"context"
	"errors"
	"testing"
	"time"

	grpcPkg "github.com/mudler/LocalAI/pkg/grpc"
	"github.com/mudler/LocalAI/pkg/system"
)

type trackingBackend struct{ grpcPkg.Backend }

func (*trackingBackend) IsBusy() bool               { return false }
func (*trackingBackend) Free(context.Context) error { return nil }

type trackingUnloader struct{ err error }

func (u trackingUnloader) UnloadRemoteModel(string) error { return u.err }

func trackedLoader() (*ModelLoader, *WatchDog) {
	ml := NewModelLoader(&system.SystemState{})
	wd := NewWatchDog(WithProcessManager(ml), WithLRULimit(1))
	ml.SetWatchDog(wd)
	ml.store.Set("a", NewModelWithClient("a", "addr-a", &trackingBackend{}))
	wd.AddAddressModelMap("addr-a", "a")
	wd.Add("addr-a", nil)
	wd.RegisterModelSize("a", 123)
	wd.Mark("addr-a")
	wd.UnMark("addr-a")
	return ml, wd
}

func TestSuccessfulUnloadClearsTracking(t *testing.T) {
	for _, force := range []bool{false, true} {
		ml, wd := trackedLoader()
		ml.remoteUnloader = trackingUnloader{}
		if err := ml.ShutdownModelContext(context.Background(), "a", force); err != nil {
			t.Fatal(err)
		}
		if wd.GetLoadedModelCount() != 0 || len(wd.addressMap) != 0 ||
			len(wd.idleTime) != 0 || len(wd.lastUsed) != 0 || len(wd.modelSizes) != 0 {
			t.Fatal("confirmed unload retained runtime tracking")
		}
		if result := wd.EnforceLRULimit(0); result.EvictedCount != 0 || result.NeedMore {
			t.Fatal("next load tried to evict an already unloaded model", result)
		}
	}
}

func TestFailedUnloadPreservesTracking(t *testing.T) {
	ml, wd := trackedLoader()
	want := errors.New("remote stop failed")
	ml.remoteUnloader = trackingUnloader{err: want}
	if err := ml.ShutdownModelContext(context.Background(), "a", false); !errors.Is(err, want) {
		t.Fatalf("expected original failure, got %v", err)
	}
	if wd.GetLoadedModelCount() != 1 || wd.addressModelMap["addr-a"] != "a" || len(wd.modelSizes) != 1 {
		t.Fatal("failed unload erased runtime evidence")
	}
}

func TestForgetModelClearsRequestsAndPreservesOtherModels(t *testing.T) {
	_, wd := trackedLoader()
	wd.SetPinnedModels([]string{"a"})
	wd.ReplaceModelGroups(map[string][]string{"a": {"gpu"}})
	wd.AddAddressModelMap("addr-b", "b")
	wd.Add("addr-b", nil)
	wd.RegisterModelSize("b", 456)
	wd.Mark("addr-a")
	finish := wd.TrackRequest("addr-a")
	wd.ForgetModel("a")
	finish()
	finish()
	wd.UnMark("addr-a")
	wd.ForgetModel("a")
	if len(wd.busyTime) != 0 || len(wd.inFlight) != 0 || len(wd.requestStarts) != 0 || len(wd.legacyRequests) != 0 {
		t.Fatal("request tracking survived or a late callback recreated it")
	}
	if wd.GetLoadedModelCount() != 1 || wd.addressModelMap["addr-b"] != "b" || wd.modelSizes["b"] != 456 {
		t.Fatal("forgetting a changed unrelated model tracking")
	}
	if !wd.IsModelPinned("a") || len(wd.GetModelGroups("a")) != 1 {
		t.Fatal("runtime cleanup removed persistent policy")
	}
}

func TestForgetModelRetainsReusedAddress(t *testing.T) {
	_, wd := trackedLoader()
	wd.AddAddressModelMap("addr-a", "b")
	wd.ForgetModel("a")
	if wd.addressModelMap["addr-a"] != "b" {
		t.Fatal("cleanup removed a different model that reused the address")
	}
	if _, retained := wd.modelSizes["a"]; retained {
		t.Fatal("cleanup retained the unloaded model size")
	}
}

func TestUnloadTrackingContinuousSwitch(t *testing.T) {
	ml, wd := trackedLoader()
	for i := 0; i < 20; i++ {
		name, addr := "a", "addr-a"
		if i%2 != 0 {
			name, addr = "b", "addr-b"
		}
		if i != 0 {
			if result := wd.EnforceLRULimit(0); result.EvictedCount != 0 {
				t.Fatal("stale LRU entry", i)
			}
			ml.store.Set(name, NewModelWithClient(name, addr, &trackingBackend{}))
			wd.AddAddressModelMap(addr, name)
		}
		wd.idleTime[addr] = time.Now()
		if err := ml.ShutdownModelContext(context.Background(), name, false); err != nil {
			t.Fatal(i, err)
		}
		if wd.GetLoadedModelCount() != 0 {
			t.Fatal("switch left tracking", i)
		}
	}
}
