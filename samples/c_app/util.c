#include <stdio.h>
#include "util.h"

int add(int a, int b) { return a + b; }

void print_result(int v)
{
    if (v > 10) {
        printf("big %d\n", v);
    } else {
        printf("small %d\n", v);
    }
}
