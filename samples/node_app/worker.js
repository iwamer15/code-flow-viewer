function heavy(ms) {
  const end = Date.now() + ms;
  let x = 0;
  while (Date.now() < end) x += Math.sqrt(x + 1);
  return x;
}

function processJob(job) {
  if (job.size > 70) return 'skip';
  heavy(job.size);
  return 'ok';
}

module.exports = { processJob };
