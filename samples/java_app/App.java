import java.util.List;

public class App {
    public static void main(String[] args) {
        Service svc = new Service("demo");
        List<String> items = svc.fetch();
        for (String s : items) {
            if (s.isEmpty()) {
                continue;
            }
            svc.handle(s);
        }
    }
}

class Service {
    private final String name;

    Service(String name) {
        this.name = name;
        init();
    }

    private void init() { }

    public List<String> fetch() {
        return List.of("a", "", "b");
    }

    public void handle(String s) {
        try {
            System.out.println(format(s));
        } catch (Exception e) {
            e.printStackTrace();
        } finally {
            cleanup();
        }
    }

    private String format(String s) { return "[" + s + "]"; }
    private void cleanup() { }
}
