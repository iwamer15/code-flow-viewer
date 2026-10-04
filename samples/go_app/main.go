package main

import (
	"fmt"
	"os"
)

type Job struct{ Name string }

func (j *Job) Run() error {
	if j.Name == "" {
		return fmt.Errorf("empty")
	}
	process(j.Name)
	return nil
}

func process(name string) {
	for i := 0; i < 3; i++ {
		fmt.Println(name, i)
	}
}

func main() {
	jobs := loadJobs(os.Args[1:])
	for _, j := range jobs {
		if err := j.Run(); err != nil {
			fmt.Println(err)
		}
	}
}
