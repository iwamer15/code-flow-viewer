package main

func loadJobs(args []string) []*Job {
	out := []*Job{}
	for _, a := range args {
		out = append(out, &Job{Name: a})
	}
	return out
}
