class JobQueue {
  constructor(jobs) { this.jobs = jobs.slice(); }
  isEmpty() { return this.jobs.length === 0; }
  next() { return this.jobs.shift(); }
}
module.exports = { JobQueue };
