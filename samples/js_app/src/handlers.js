class UserStore {
  constructor() { this.users = []; }
  add(u) { this.users.push(u); return u; }
  all() { return this.users; }
}
const store = new UserStore();
exports.home = function () { return 'home'; };
exports.createUser = () => { return JSON.stringify(store.add({ id: Date.now() })); };
exports.listUsers = async () => {
  try {
    return JSON.stringify(store.all());
  } catch (e) {
    return '[]';
  }
};
