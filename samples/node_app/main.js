// サンプル: キューに積んだ仕事を順に処理する (▶ 実行 → Node.js で進み具合を確認できます)
const { JobQueue } = require('./queue');
const { processJob } = require('./worker');

function loadJobs(n) {
  const jobs = [];
  for (let i = 1; i <= n; i++) jobs.push({ id: i, size: 20 + (i % 3) * 30 });
  return jobs;
}

async function main() {
  const queue = new JobQueue(loadJobs(Number(process.argv[2] || 6)));
  while (!queue.isEmpty()) {
    const job = queue.next();
    const result = processJob(job);
    console.log(`job ${job.id}: ${result}`);
    await new Promise((r) => setTimeout(r, 150));
  }
  console.log('done');
}

main();
