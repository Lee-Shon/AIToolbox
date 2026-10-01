package nodes

// This extension belongs to LocalAI's node owner. The composition gateway must
// neither write these records nor issue independent physical resource permits.
import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"strings"
	"time"

	"github.com/google/uuid"
	pb "github.com/mudler/LocalAI/pkg/grpc/proto"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"google.golang.org/protobuf/proto"
)

var ErrResourceObservationUnknown = errors.New("resource observation unknown")
var ErrResourceWaiting = errors.New("waiting for physical resources")
var ErrResourceProfileRequired = errors.New("validated resource profile required")
var ErrResourceStopUnconfirmed = errors.New("physical resource release is not confirmed")

// NativeResourceProfile is measured for one immutable native configuration,
// including its batch and slot capacities. Peak includes weights, attachments,
// KV and workspace; ResidentFloor is a proven lower bound, not a reading of zero
// when telemetry is unavailable. Missing per-process observations remain nil.
type NativeResourceProfile struct {
	Revision string `json:"revision"`
	EvidenceSHA256 string `json:"evidence_sha256"`
	ConfigurationSHA256 string `json:"configuration_sha256"`
	ResidentFloorVRAM uint64 `json:"resident_floor_vram"`
	PeakVRAM uint64 `json:"peak_vram"`
	ResidentFloorRAM uint64 `json:"resident_floor_ram"`
	PeakRAM uint64 `json:"peak_ram"`
	Slots int `json:"slots"`
	Batch int `json:"batch"`
}

// Allocations outlive registry removal, heartbeat loss and request completion.
// Only a matching physical-stop receipt can release one. Released rows retain
// the policy, demand, process identity and evidence needed to audit the decision.
type NativeAllocation struct {
	ID string `gorm:"primaryKey;size:36" json:"id"`
	NodeID string `gorm:"index;size:36" json:"node_id"`
	ModelName string `gorm:"index;size:255" json:"model_name"`
	ReplicaIndex int `json:"replica_index"`
	ConfigRevision string `json:"config_revision"`
	Address string `json:"address"`
	State string `gorm:"index" json:"state"`
	ProfileJSON string `gorm:"type:text" json:"profile_json"`
	PeakVRAM uint64 `json:"predicted_peak_vram"`
	PeakRAM uint64 `json:"predicted_peak_ram"`
	ResidentFloorVRAM uint64 `json:"resident_floor_vram"`
	ResidentFloorRAM uint64 `json:"resident_floor_ram"`
	ObservedVRAM *uint64 `json:"observed_vram"`
	ObservedRAM *uint64 `json:"observed_ram"`
	ObservedPeakVRAM *uint64 `json:"observed_peak_vram"`
	ObservedPeakRAM *uint64 `json:"observed_peak_ram"`
	PolicyRevision string `json:"policy_revision"`
	EvictionProtected bool `json:"eviction_protected"`
	LoadObservationSequence uint64 `json:"load_observation_sequence"`
	LoadVRAMObservationSequence uint64 `gorm:"column:load_vram_observation_sequence" json:"load_vram_observation_sequence"`
	StopEvidence string `gorm:"type:text" json:"stop_evidence,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
	ReleasedAt *time.Time `json:"released_at"`
}

type NativeResourcePolicy struct {
	Revision string `json:"revision"`
	RAMBudget uint64 `json:"ram_budget"`
	RAMHeadroom uint64 `json:"ram_headroom"`
	VRAMHeadroom uint64 `json:"vram_headroom"`
	ObservationMaxAge time.Duration `json:"observation_max_age_ns"`
}

func nativeConfigurationHash(opts *pb.ModelOptions) (string, error) {
	if opts == nil { return "", ErrResourceProfileRequired }
	// Sampling seed is randomized by native getSeed on each request and does
	// not change this bounded allocation shape. It remains in the separate
	// native execution/config identity. All capacity options stay in this hash.
	copyOpts := proto.Clone(opts).(*pb.ModelOptions)
	copyOpts.Seed = 0
	copyOpts.Options = nil
	for _, option := range opts.Options {
		if !strings.HasPrefix(option, "resource_profile:") { copyOpts.Options = append(copyOpts.Options, option) }
	}
	b, err := json.Marshal(copyOpts)
	if err != nil { return "", err }
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:]), nil
}

func nativeProfile(opts *pb.ModelOptions) (NativeResourceProfile, error) {
	var p NativeResourceProfile
	if opts == nil { return p, ErrResourceProfileRequired }
	found := false
	for _, option := range opts.Options {
		if strings.HasPrefix(option, "resource_profile:") {
			if found { return p, fmt.Errorf("duplicate resource profile") }
			found = true
			d := json.NewDecoder(strings.NewReader(strings.TrimPrefix(option, "resource_profile:")))
			d.DisallowUnknownFields()
			if err := d.Decode(&p); err != nil { return p, err }
			if err := d.Decode(new(any)); err != io.EOF { return p, fmt.Errorf("resource profile must contain exactly one JSON value") }
		}
	}
	if !found || p.Revision == "" || len(p.EvidenceSHA256) != 64 || p.PeakRAM == 0 || p.Slots < 1 || p.Batch < 1 {
		return p, NativeProfileRequiredError(opts)
	}
	if _, err := hex.DecodeString(p.EvidenceSHA256); err != nil { return p, ErrResourceProfileRequired }
	if p.ResidentFloorRAM > p.PeakRAM || p.ResidentFloorVRAM > p.PeakVRAM { return p, fmt.Errorf("resident floor exceeds peak") }
	if (opts.CUDA || opts.NGPULayers != 0) && p.PeakVRAM == 0 { return p, ErrResourceProfileRequired }
	identity, err := nativeConfigurationHash(opts)
	if err != nil { return p, err }
	if identity != p.ConfigurationSHA256 { return p, fmt.Errorf("resource evidence invalidated by native configuration change; configuration_sha256=%s",identity) }
	return p, nil
}

func addResource(a, b uint64) (uint64, error) {
	if b > math.MaxUint64-a { return 0, fmt.Errorf("resource arithmetic overflow") }
	return a+b, nil
}

// nativeFit checks both the policy pool and measured physical availability.
// Existing weights are charged once. Outstanding workspace and incomplete
// loads are deducted from free memory even if a heartbeat arrives meanwhile.
func nativeFit(n BackendNode, allocations []NativeAllocation, p NativeResourceProfile, policy NativeResourcePolicy, now time.Time) error {
	if policy.Revision == "" || policy.ObservationMaxAge <= 0 { return fmt.Errorf("explicit resource policy required") }
	if n.Status != StatusHealthy || n.LastHeartbeat.IsZero() || now.Sub(n.LastHeartbeat) > policy.ObservationMaxAge || n.LastHeartbeat.After(now.Add(time.Second)) {
		return fmt.Errorf("node unavailable or stale: %w", ErrResourceObservationUnknown)
	}
	if n.TotalRAM == 0 || (p.PeakVRAM > 0 && n.TotalVRAM == 0) { return ErrResourceObservationUnknown }
	if n.NativeRAMObservedAt == nil || now.Sub(*n.NativeRAMObservedAt)>policy.ObservationMaxAge { return ErrResourceObservationUnknown }
	ramBudget := n.TotalRAM
	if policy.RAMBudget > 0 && policy.RAMBudget < ramBudget { ramBudget = policy.RAMBudget }
	vramBudget := n.TotalVRAM
	if n.VRAMBudgetBytes > 0 && n.VRAMBudgetBytes < vramBudget { vramBudget = n.VRAMBudgetBytes }
	reservedV, reservedR := p.PeakVRAM, p.PeakRAM
	pendingV, pendingR := p.PeakVRAM, p.PeakRAM
	for _, a := range allocations {
		if a.State == "released" { continue }
		var err error
		if reservedV, err = addResource(reservedV, a.PeakVRAM); err != nil { return err }
		if reservedR, err = addResource(reservedR, a.PeakRAM); err != nil { return err }
		floorV, floorR := uint64(0), uint64(0)
		// A pre-load heartbeat still shows the weights as free memory. Wait
		// for two subsequent serial worker observations before discounting
		// resident weights; the first may already have been in transit.
		if a.State == "live" {
			if n.NativeObservationSequence > a.LoadObservationSequence && n.NativeObservationSequence-a.LoadObservationSequence >= 2 { floorR=a.ResidentFloorRAM }
			if n.NativeVRAMObservationSequence > a.LoadVRAMObservationSequence && n.NativeVRAMObservationSequence-a.LoadVRAMObservationSequence >= 2 { floorV=a.ResidentFloorVRAM }
		}
		if a.PeakVRAM < floorV || a.PeakRAM < floorR { return fmt.Errorf("corrupt resource allocation") }
		if pendingV, err = addResource(pendingV, a.PeakVRAM-floorV); err != nil { return err }
		if pendingR, err = addResource(pendingR, a.PeakRAM-floorR); err != nil { return err }
	}
	fit := func(need, available, margin uint64) bool { return available >= margin && need <= available-margin }
	if reservedV > 0 && (n.TotalVRAM == 0 || n.NativeVRAMObservedAt == nil || now.Sub(*n.NativeVRAMObservedAt)>policy.ObservationMaxAge) { return ErrResourceObservationUnknown }
	if !fit(reservedR, ramBudget, policy.RAMHeadroom) || !fit(pendingR, n.AvailableRAM, policy.RAMHeadroom) { return fmt.Errorf("RAM budget or external pressure: %w", ErrResourceWaiting) }
	if reservedV > 0 && (!fit(reservedV, vramBudget, policy.VRAMHeadroom) || !fit(pendingV, n.AvailableVRAM, policy.VRAMHeadroom)) {
		return fmt.Errorf("VRAM budget or external pressure: %w", ErrResourceWaiting)
	}
	return nil
}

// ReserveNativeResources serializes only decisions for this physical node.
// Native lifecycle operations and this claim share the database; heartbeats
// cannot erase the allocation. No backend may be installed before this commits.
func (r *NodeRegistry) ReserveNativeResources(ctx context.Context, nodeID, modelName, revision string, p NativeResourceProfile, policy NativeResourcePolicy, protected ...bool) (*NativeAllocation, error) {
	var result NativeAllocation
	err := r.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var node BackendNode
		if err := tx.Clauses(clause.Locking{Strength:"UPDATE"}).First(&node, "id = ?", nodeID).Error; err != nil { return err }
		var held []NativeAllocation
		if err := tx.Where("node_id = ? AND state <> ?", nodeID, "released").Find(&held).Error; err != nil { return err }
		var nativeInstances []NodeModel
		if err := tx.Where("node_id = ?", nodeID).Find(&nativeInstances).Error; err != nil { return err }
		for _, instance := range nativeInstances {
			accounted := false
			for _, a := range held {
				if a.ModelName == instance.ModelName && a.ReplicaIndex == instance.ReplicaIndex && a.Address == instance.Address { accounted = true; break }
			}
			if !accounted { return fmt.Errorf("native instance %s is not accounted: %w", instance.ModelName, ErrResourceObservationUnknown) }
		}
		if err := nativeFit(node, held, p, policy, time.Now()); err != nil { return err }
		if nativeResourcesEnabled() {
			older, err := nativeOlderWait(tx,node,held,modelName,policy)
			if err != nil { return err }; if older != nil { return fmt.Errorf("older feasible model is waiting: %w",ErrResourceWaiting) }
		}
		maxSlots := max(node.MaxReplicasPerModel, 1)
		used := make(map[int]bool)
		for _, a := range held { if a.ModelName == modelName { used[a.ReplicaIndex] = true } }
		var existing []NodeModel
		if err := tx.Where("node_id = ? AND model_name = ?", nodeID, modelName).Find(&existing).Error; err != nil { return err }
		for _, row := range existing { used[row.ReplicaIndex] = true }
		slot := 0
		for used[slot] { slot++ }
		if slot >= maxSlots { return ErrNoFreeSlot }
		b, err := json.Marshal(p)
		if err != nil { return err }
		result = NativeAllocation{ID:uuid.NewString(), NodeID:nodeID, ModelName:modelName, ReplicaIndex:slot,
			ConfigRevision:revision, State:"reserved", ProfileJSON:string(b), PeakVRAM:p.PeakVRAM, PeakRAM:p.PeakRAM,
			ResidentFloorVRAM:p.ResidentFloorVRAM, ResidentFloorRAM:p.ResidentFloorRAM, PolicyRevision:policy.Revision}
		if len(protected)>0 { result.EvictionProtected=protected[0] }
		return tx.Create(&result).Error
	})
	return &result, err
}

func (r *NodeRegistry) NativeAllocationInstalled(ctx context.Context, id, address string) error {
	if address == "" { return fmt.Errorf("native process address required") }
	res := r.db.WithContext(ctx).Model(&NativeAllocation{}).Where("id = ? AND state = ? AND address = ?", id, "reserved", "").Update("address", address)
	if res.Error != nil { return res.Error }
	if res.RowsAffected != 1 { return fmt.Errorf("native install identity conflict") }
	return nil
}

// Only this exact instance can transition. Inference completion intentionally
// has no release operation: its loaded weights are still owned and reusable.
func (r *NodeRegistry) NativeAllocationLoaded(ctx context.Context, id, address string) error {
	if address == "" { return fmt.Errorf("native process address required") }
	return r.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var a NativeAllocation
		if err:=tx.First(&a,"id = ?",id).Error;err!=nil{return err}
		var node BackendNode
		if err:=tx.Clauses(clause.Locking{Strength:"UPDATE"}).First(&node,"id = ?",a.NodeID).Error;err!=nil{return err}
		res := tx.Model(&NativeAllocation{}).Where("id = ? AND state = ? AND address = ?", id, "reserved", address).Updates(map[string]any{"state":"live","load_observation_sequence":node.NativeObservationSequence,"load_vram_observation_sequence":node.NativeVRAMObservationSequence})
		if res.Error != nil { return res.Error }
		if res.RowsAffected != 1 { return fmt.Errorf("native allocation transition conflict") }
		return nil
	})
}

func (r *NodeRegistry) ReleaseNativeResources(ctx context.Context, id, address, stopEvidence string, terminated bool) error {
	if !terminated || address == "" || stopEvidence == "" { return ErrResourceStopUnconfirmed }
	return r.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var a NativeAllocation
		if err := tx.Clauses(clause.Locking{Strength:"UPDATE"}).First(&a, "id = ?", id).Error; err != nil { return err }
		if a.Address != address { return fmt.Errorf("stop receipt belongs to another native process") }
		if a.State == "released" { return nil }
		now := time.Now()
		if err := tx.Model(&a).Updates(map[string]any{"state":"released", "released_at":now, "stop_evidence":stopEvidence}).Error; err != nil { return err }
		return tx.Model(&NativeCallReservation{}).Where("allocation_id = ? AND state IN ?", a.ID, []string{"active","unknown"}).Update("state","stopped").Error
	})
}
