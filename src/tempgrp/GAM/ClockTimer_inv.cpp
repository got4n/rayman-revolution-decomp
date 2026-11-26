#include "common.h"

extern "C" {
#include "TMR/TMR.h"

extern inv_tdstTimer0* inv_TMR_p_stTimer0;
extern s32 inv_TMR_lTimer0RefCount;
}

extern "C" inv_tdstTimer0* func_0014B830(u32 requested)
{
    if (inv_TMR_p_stTimer0 == 0) {
        inv_TMR_p_stTimer0 = new inv_tdstTimer0(requested);
        inv_TMR_lTimer0RefCount = 0;
    }
    inv_TMR_lTimer0RefCount++;
    return inv_TMR_p_stTimer0;
}

extern "C" void func_0014B8B8(void)
{
    if (--inv_TMR_lTimer0RefCount == 0 && inv_TMR_p_stTimer0 != 0) {
        inv_TMR_fn_vTimer0Shutdown(inv_TMR_p_stTimer0, 3);
        inv_TMR_p_stTimer0 = 0;
    }
}
