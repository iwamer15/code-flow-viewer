pub struct Engine { n: u32 }

impl Engine {
    pub fn new(n: u32) -> Self { Engine { n } }

    pub fn run(&mut self) -> Result<u32, &'static str> {
        if self.n == 0 {
            return Err("zero");
        }
        Ok(self.tick())
    }

    fn tick(&self) -> u32 { self.n * 2 }
}
