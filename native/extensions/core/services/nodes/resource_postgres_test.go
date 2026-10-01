package nodes

import (
 "context"
 "encoding/json"
 "errors"
 "fmt"
 "os"
 "sync"
 "strings"
 "testing"

 "github.com/google/uuid"
 "gorm.io/driver/postgres"
 "gorm.io/gorm"
)

// Run only against a disposable database supplied by the build harness. The
// test creates its own schema and never touches the live LocalAI tables.
func TestNativeResourcePostgresConcurrentReservation(t *testing.T){
 dsn:=os.Getenv("LOCALAI_RESOURCE_TEST_POSTGRES");if dsn==""{t.Skip("disposable PostgreSQL DSN required")}
 schema:="resource_"+strings.ReplaceAll(uuid.NewString(),"-","")
 root,err:=gorm.Open(postgres.Open(dsn),&gorm.Config{});if err!=nil{t.Fatal(err)}
 if err:=root.Exec("CREATE SCHEMA "+schema).Error;err!=nil{t.Fatal(err)}
 defer root.Exec("DROP SCHEMA "+schema+" CASCADE")
 db,err:=gorm.Open(postgres.Open(dsn+" search_path="+schema),&gorm.Config{});if err!=nil{t.Fatal(err)}
 if err:=db.AutoMigrate(&BackendNode{},&NodeModel{},&NativeAllocation{},&NativeCallReservation{},&NativeResourceWait{});err!=nil{t.Fatal(err)}
 n:=resourceTestNode();n.ID=uuid.NewString();n.VRAMBudgetBytes=8
 if err:=db.Create(&n).Error;err!=nil{t.Fatal(err)}
 policy,_:=json.Marshal(resourceTestPolicy());t.Setenv("LOCALAI_RESOURCE_POLICY",string(policy))
 r:=&NodeRegistry{db:db}
 start:=make(chan struct{});results:=make(chan error,32);var workers sync.WaitGroup
 for i:=0;i<32;i++{workers.Add(1);go func(index int){defer workers.Done();<-start;_,err:=r.ReserveNativeResources(context.Background(),n.ID,fmt.Sprintf("model-%d",index),"revision",resourceTestProfile(),resourceTestPolicy());results<-err}(i)}
 close(start);workers.Wait();close(results)
 admitted:=0
 for err:=range results{if err==nil{admitted++}else if !errors.Is(err,ErrResourceWaiting){t.Error(err)}}
 if admitted!=1{t.Fatalf("physical node lock allowed %d concurrent claims for one available allocation",admitted)}
 var rows int64
 if err:=db.Model(&NativeAllocation{}).Count(&rows).Error;err!=nil{t.Fatal(err)}
 if rows!=1{t.Fatalf("durable allocations = %d",rows)}
}
