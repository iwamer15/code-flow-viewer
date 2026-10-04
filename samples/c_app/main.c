#include "util.h"

static int compute(int n) {
    int s = 0;
    while (n-- > 0) {
        s = add(s, n);
    }
    return s;
}

int main(int argc, char **argv) {
    int r = compute(argc * 3);
    print_result(r);
    return 0;
}
