#include "common.h"

#include "TMR/TMR.h"

s32 fn_vTimerWait(s32 ulTicksToWait)
{
    return TMR_fn_wWait(inv_GAM_engineClockState, ulTicksToWait);
}
