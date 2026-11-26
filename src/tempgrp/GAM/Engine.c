#include "common.h"
#include "GAM/GAM.h"

void func_001000D8(void);

void inv_j_fn_vChooseTheGoodDesInit(void)
{
    fn_vChooseTheGoodDesInit();
}

s32 inv_j_IPT_fn_bIsKeyboardControlAvailable(void)
{
    return IPT_fn_bIsKeyboardControlAvailable();
}

s32 inv_j_fn_bIsEngineModeChangeRequested(void)
{
    return inv_fn_bIsEngineModeChangeRequested();
}

void func_00100128(void)
{
    fn_vOneTrame();
}

void inv_fn_vMainEngineLoop(void)
{
    while (!inv_j_IPT_fn_bIsKeyboardControlAvailable()) {
        func_001000D8();
        inv_GAM_ulFramesInMode = 0;
        while (!inv_j_fn_bIsEngineModeChangeRequested()) {
            func_00100128();
        }
        inv_j_fn_vChooseTheGoodDesInit();
    }
}
