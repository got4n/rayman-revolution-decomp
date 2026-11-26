extern void fn_pDispatchServerFunc_inv(void);
extern int fn_iStartServer_inv(void *p);
extern int fn_iInitIOPCmdModule_inv(void);
extern void loadcore_5_inv(void);
extern int intrman_9_inv(void);

typedef struct {
    int id;
    void (*entry)(void);
} Pair_inv;

int fn_iInitSndIop_inv(void)
{
    int ret = -1;
    Pair_inv p;

    if (fn_iInitIOPCmdModule_inv() >= 0) {
        p.id = 0x65653269;
        p.entry = fn_pDispatchServerFunc_inv;
        ret = fn_iStartServer_inv(&p);
    }
    return ret;
}

int start(void)
{
    int ret = -1;

    loadcore_5_inv();
    if (intrman_9_inv() == 0) {
        if (fn_iInitSndIop_inv() >= 0)
            ret = 0;
    }
    return ret;
}
