package worker

import (
 "encoding/json"
 "fmt"
 "net"
 "os"
 "path/filepath"

 "github.com/google/uuid"
 "github.com/mudler/LocalAI/core/services/messaging"
)

// A closed generation is a durable fence against a delayed install message.
// Holding the per-slot lock across install and stop prevents an absent process
// from being mistaken for a stopped process while its installer is still live.
type allocationGeneration struct {
 ID string `json:"id"`
 ProcessKey string `json:"process_key"`
 Address string `json:"address"`
 State string `json:"state"`
}

func generationPath(id string) (string,error) {
 if _,err:=uuid.Parse(id);err!=nil{return "",fmt.Errorf("invalid allocation identity")}
 root:=os.Getenv("LOCALAI_ALLOCATION_JOURNAL")
 if root==""{return "",fmt.Errorf("durable allocation journal required")}
 if err:=os.MkdirAll(root,0700);err!=nil{return "",err}
 return filepath.Join(root,id+".json"),nil
}

func readGeneration(id string)(allocationGeneration,error){
 var g allocationGeneration
 p,err:=generationPath(id);if err!=nil{return g,err}
 b,err:=os.ReadFile(p);if err!=nil{return g,err}
 err=json.Unmarshal(b,&g);return g,err
}

func saveGeneration(g allocationGeneration)error{
 p,err:=generationPath(g.ID);if err!=nil{return err}
 b,err:=json.Marshal(g);if err!=nil{return err}
 f,err:=os.CreateTemp(filepath.Dir(p),".generation-");if err!=nil{return err}
 defer os.Remove(f.Name())
 if _,err=f.Write(b);err!=nil{f.Close();return err}
 if err=f.Sync();err!=nil{f.Close();return err};if err=f.Close();err!=nil{return err}
 if err=os.Rename(f.Name(),p);err!=nil{return err}
 d,err:=os.Open(filepath.Dir(p));if err!=nil{return err};defer d.Close();return d.Sync()
}

func (s *backendSupervisor) handleReservedBackendInstall(data []byte,reply func([]byte)) {
 go func(){
  var req messaging.BackendInstallRequest
  if err:=json.Unmarshal(data,&req);err!=nil{replyJSON(reply,messaging.BackendInstallReply{Error:err.Error()});return}
  result:=messaging.BackendInstallReply{AllocationID:req.AllocationID}
  fail:=func(err error){result.Error=err.Error();replyJSON(reply,result)}
  if req.AllocationID==""||req.Force||req.ModelID==""{fail(fmt.Errorf("reserved install requires immutable allocation and model identity"));return}
  key:=buildProcessKey(req.ModelID,req.Backend,int(req.ReplicaIndex))
  releaseSlot:=s.lockBackend("allocation:"+key);defer releaseSlot()
  generation,err:=readGeneration(req.AllocationID)
  if err!=nil&&!os.IsNotExist(err){fail(err);return}
  if err==nil && (generation.ProcessKey!=key||generation.State=="closed"){fail(fmt.Errorf("allocation generation is closed or belongs to another slot"));return}
  s.mu.Lock();bp:=s.processes[key]
  conflict:=bp!=nil&&bp.allocationID!=req.AllocationID
  s.mu.Unlock()
  if conflict{fail(fmt.Errorf("native slot belongs to another allocation"));return}
  if bp==nil && err==nil && generation.State!="reserved" {
   // A worker restart alone is not proof that a previously launched child
   // terminated. The original generation must be reconciled, not relaunched.
   fail(fmt.Errorf("previous native process generation requires reconciliation"));return
  }
  generation=allocationGeneration{ID:req.AllocationID,ProcessKey:key,State:"launching"}
  if err:=saveGeneration(generation);err!=nil{fail(err);return}
  releaseBackend:=s.lockBackend(req.Backend);defer releaseBackend()
  address,installErr:=s.installBackend(req,false)
  s.mu.Lock();bp=s.processes[key]
  if bp!=nil{bp.allocationID=req.AllocationID}
  s.mu.Unlock()
  if bp!=nil{generation.State="launched";generation.Address=bp.addr}
  if err:=saveGeneration(generation);err!=nil{fail(err);return}
  if installErr!=nil{fail(installErr);return}
  host,_,err:=net.SplitHostPort(s.cfg.advertiseAddr());if err!=nil{fail(err);return}
  _,port,err:=net.SplitHostPort(address);if err!=nil{fail(err);return}
  result.Success=true;result.Address=net.JoinHostPort(host,port);replyJSON(reply,result)
 }()
}

// Called under the generation lock by stopModelExact before it examines the
// process map. Missing *new* generations are fenced before acknowledging;
// missing previously-launched generations need a physical restart receipt.
func prepareGenerationStop(req messaging.ModelStopRequest,bp *backendProcess)(bool,error){
 if req.AllocationID==""{return false,nil}
 g,err:=readGeneration(req.AllocationID)
 if err!=nil&&!os.IsNotExist(err){return false,err}
 if err==nil && g.ProcessKey!=req.ProcessKey{return false,fmt.Errorf("allocation slot mismatch")}
 if err==nil && g.State=="closed"{return true,nil}
 if bp==nil && err==nil && (g.State=="launched"||g.State=="launching"){return false,fmt.Errorf("missing launched generation is not a physical stop receipt")}
 if bp!=nil && bp.allocationID!=req.AllocationID{return false,fmt.Errorf("allocation generation mismatch")}
 if bp==nil {
  return true,saveGeneration(allocationGeneration{ID:req.AllocationID,ProcessKey:req.ProcessKey,State:"closed"})
 }
 return false,nil
}
