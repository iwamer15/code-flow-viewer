mod engine;
use engine::Engine;

fn main() {
    let mut e = Engine::new(4);
    let r = e.run();
    match r {
        Ok(v) => println!("ok {}", v),
        Err(err) => report(&err),
    }
}

fn report<'a>(msg: &'a str) {
    eprintln!("{}", msg);
}
