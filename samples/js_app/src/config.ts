export interface Config { port: number }
export function loadConfig(): Config {
  const port = parsePort(process.env.PORT);
  return { port };
}
function parsePort(v?: string): number {
  return v ? parseInt(v, 10) : 3000;
}
