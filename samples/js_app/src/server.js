const http = require('http');
const { route } = require('./router');
import { loadConfig as cfg } from './config';

const config = cfg();

function startServer(port) {
  const server = http.createServer((req, res) => {
    const out = route(req.url, req.method);
    res.end(out);
  });
  server.listen(port, () => logStart(port));
  return server;
}

const logStart = (port) => {
  console.log(`listening on ${port}`); // log(
};

startServer(config.port);
