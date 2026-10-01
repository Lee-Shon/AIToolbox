package worker

import (
 "testing"

 "github.com/google/uuid"
 "github.com/mudler/LocalAI/core/services/messaging"
)

func TestNativeGenerationRequiresDurableJournal(t *testing.T){
 t.Setenv("LOCALAI_ALLOCATION_JOURNAL","")
 if _,err:=generationPath(uuid.NewString());err==nil{t.Fatal("missing durable journal accepted")}
 t.Setenv("LOCALAI_ALLOCATION_JOURNAL",t.TempDir())
 if _,err:=generationPath("../bad");err==nil{t.Fatal("invalid generation path accepted")}
}

func TestNativeGenerationLateInstallFenceAndUnknownRestart(t *testing.T){
 t.Setenv("LOCALAI_ALLOCATION_JOURNAL",t.TempDir())
 id:=uuid.NewString();req:=messaging.ModelStopRequest{AllocationID:id,ProcessKey:"asr#0"}
 stopped,err:=prepareGenerationStop(req,nil)
 if err!=nil||!stopped{t.Fatalf("new generation was not durably fenced: %v",err)}
 g,err:=readGeneration(id);if err!=nil||g.State!="closed"{t.Fatal("closed generation lost")}
 id=uuid.NewString();req.AllocationID=id
 if err:=saveGeneration(allocationGeneration{ID:id,ProcessKey:req.ProcessKey,State:"launching"});err!=nil{t.Fatal(err)}
 if stopped,err=prepareGenerationStop(req,nil);err==nil||stopped{t.Fatal("unknown dispatch became confirmed stop")}
 if stopped,err=prepareGenerationStop(req,&backendProcess{allocationID:uuid.NewString()});err==nil||stopped{t.Fatal("replacement generation was targeted")}
}
