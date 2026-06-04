package app

import (
    "fmt"
    "net/http"
)

func Ping() {
    fmt.Println(http.MethodGet)
}
