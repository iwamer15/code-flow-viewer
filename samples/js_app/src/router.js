const handlers = require('./handlers');

function route(url, method) {
  switch (url) {
    case '/':
      return handlers.home();
    case '/users':
      if (method === 'POST') {
        return handlers.createUser();
      } else {
        return handlers.listUsers();
      }
    default:
      return notFound(url);
  }
}

function notFound(url) { return '404 ' + url; }

module.exports = { route };
