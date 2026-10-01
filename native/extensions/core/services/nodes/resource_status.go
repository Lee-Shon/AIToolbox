package nodes

import (
 "context"
 "fmt"

 pb "github.com/mudler/LocalAI/pkg/grpc/proto"
 "google.golang.org/protobuf/proto"
)

func (r *NodeRegistry) NativeResourceStatus(ctx context.Context)(map[string]any,error){
 policy,enabled,err:=nativeResourcePolicy();if err!=nil{return nil,err}
 nodes,err:=r.List(ctx);if err!=nil{return nil,err}
 var allocations []NativeAllocation
 if err:=r.db.WithContext(ctx).Where("state <> ?","released").Order("created_at ASC").Find(&allocations).Error;err!=nil{return nil,err}
 var calls []NativeCallReservation
 if err:=r.db.WithContext(ctx).Where("state IN ?",[]string{"active","unknown"}).Order("created_at ASC").Find(&calls).Error;err!=nil{return nil,err}
 var waiting []NativeResourceWait
 if err:=r.db.WithContext(ctx).Order("created_at ASC").Find(&waiting).Error;err!=nil{return nil,err}
 return map[string]any{"owner":"localai","protocol":"native-resources/1","enabled":enabled,"policy":policy,
  "nodes":nodes,"allocations":allocations,"calls":calls,"waiting":waiting,
  "observation_note":"predicted peaks and measured values are separate; null process measurements are unknown"},nil
}

func (r *NodeRegistry) NativeResourceConfiguration(ctx context.Context,model string)(map[string]any,error){
 backend,revision,blob,err:=r.GetModelLoadInfoRevision(ctx,model);if err!=nil{return nil,err}
 var opts pb.ModelOptions
 if err:=proto.Unmarshal(blob,&opts);err!=nil{return nil,err}
 hash,err:=nativeConfigurationHash(&opts);if err!=nil{return nil,err}
 return map[string]any{"model":model,"backend":backend,"config_revision":revision,"configuration_sha256":hash,
  "context_size":opts.ContextSize,"batch":opts.NBatch,"gpu_layers":opts.NGPULayers,
  "status":"native loaded configuration identity; calibration evidence is required separately"},nil
}

func NativeProfileRequiredError(opts *pb.ModelOptions)error{
 hash,err:=nativeConfigurationHash(opts);if err!=nil{return err}
 return fmt.Errorf("configuration_sha256=%s: %w",hash,ErrResourceProfileRequired)
}
