# Rayman Revolution (PS2) - decompilation project

This project aims to decompile Rayman Revolution (PS2). This targets the NTSC-U (SLUS_201.38) build of the game (33618569b7a1aa23300e9d1139510220189b3abe). 
This is used for the native PC port of the game: https://github.com/got4n/rayman2-revolution-pc

Obviously, no assets are provided, you'll have to provide the executable from your own copy of the game.

**IMPORTANT NOTE: this does not take any name, any code, anything from any leaks or any public reverse-engineering of the game (unless I could legitimately check myself where the names came from). All of the names are either invented or explained later on where they come from. If anyone wants to contribute, do NOT take anything from any leaks either.**

Right now, this only targets the UbiSoft portion code: this doesn't target the RenderWare 3 code (or the PS2 libraries) at the moment; because I'm pretty sure Ubisoft were given only the release library for Renderware (version 3.04 as far as I know) and didn't change anything and instead added their own GliGlou code that calls RenderWare (which is getting decompiled). Maybe in the future, who knows?  
Alongside it, is a decompilation of the UBISOUND.IRX module. However, since this one was done towards the end of the decompilation, it's not cleaned AT ALL, but it matches.

# Progress

| Binary | Functions | Code bytes |
|--------|-----------|------------|
| `SLUS_201.38`, game code | 3,472 / 3,472 (100%) | 702,736 / 702,736 (100%) |
| `UBISOUND.IRX` | 693 / 693 (100%) | 79,032 / 79,032 (100%) |

Small disclaimer: please read the section under talking about objdiff.

# Why Rayman Revolution?

Rayman Revolution has always been a version that interested me, it's kind of a remake in the way that it adds scrapped stuff from Rayman 2 back, it adds an overworld, it adds sections but it's also still Rayman 2; and it being PS2 exclusive and stuck in 30fps glory (unless you use that 60fps PCSX2 cheat code) kind of bothered me.
It also adds a lot of things but also added a lot of bugs (like that nasty music bug) on top of wonky physics (the most notable example being that you run faster diagonally than straight). So I thought, it'd be cool to have a decompilation so we could "fix" some of the bugs of Revolution, add some QoL features etc. 

# Does it use AI?

Unfortunately in some parts, yes. (and I hate AI as much as most people do, also fuck you Sam Altmann, fuck you Dario Amodei, fuck all the AIbros).

But... what makes it different compared to AI slop decomps? Well, you can trust me (and my love for the Rayman series) on the fact that I have worked with these games enough to at least have an idea on what I'm doing in regards to how this game works internally to at least not ***try*** to slop anything.

I'm doing my best to re-read everything that is AI, fix it or make it look cleaner on my own, improve the names and fix as much as I can! Code is still quite ugly at that point, but I'm focusing on getting the matching first and I'll try to clean up & label the code more afterwards.

Function matching avoided it as much as possible, but I'm alone on this and also not good enough with programming to code some tools in regards to some stuff like objdiff, compilation etc. If any human would like to improve or rewrite tools, functions; go ahead, I'll gladly take this over any AI code.

I know that some of you might say "if you're not good enough at it, then don't do it in the first place?", to which I answer: yeah sure, but I thought it'd be cool to use my knowledge of the engine/game to at least try to propose something to others, and if I can make 1 person happy with this project, it would already be enough for me!

# Function names

Rayman Revolution never shipped with a debugging executable or any kind of symbols.

So, how did I deal with that? I tried giving names to a good chunk of the functions. Names are kinda dirty.

Some are invented (inv_). Probably, a good chunk of these names are wrong, but I can always change the names as time goes on. Some of the Ubisoft code is still unnamed.

What about the rest? Well, Rayman Revolution, being a remake of Rayman 2, naturally uses a good chunk of Rayman 2's functions. So I based it on the iOS port which has symbols and some declarations. However, this isn't enough because Revolution uses a slightly updated version. Thus, I also picked stuff from Rayman 3's Korean PS2 version which is a DWARF executable. However (again), Revolution uses Renderware 3 for the rendering, unlike other Rayman games. So for this, I had to take inspiration from Hype: The Time Quest, another PS2 game using the same CPA engine (although a different branch of it) that also uses Renderware 3 and has a DWARF executable! 
Basically, Revolution is a mix of Hype, Rayman 2, Rayman 3 sprinkled with some Renderware 3 funsies! (sometimes identical, sometimes slightly different code, sometimes completely different)
I also used some of the names from Burnout games as their DWARF info helped a lot with naming some Renderware stuff that wasn't in Hype. (like structs)

I spent a LOT of time trying to match everything by hand, this was truly horrible and at one point I was beginning to get super burned out from it, so there might (and there will) be some errors lol.

# Stuff I'd like to improve

I will admit, some functions can fall into the "fake" matchings category: there might be small empty ASMs asm("") declarations, some do while 0 or one liners ASM because I couldn't get the functions to match without that (it could be leftover debugging code that got optimized out, or not). But these don't change the actual "code" itself in regards to what it does and they're very limited.  
There are also some raw assembly macros, but I'm confident that most (if not all) the ASM macros I used were used by Ubisoft, most notably stuff regarding mula/madda.s, and I'm pretty sure they used macros for some matrix math for example.  

Another gripe I have is that I want to fix all the partial structs, there are still quite a few, just like some raw offsets left.
Please don't be too annoyed with these!

# About objdiff's 100%

Out of the box, objdiff 3.6.1 says 3,319 / 3,468 functions (95.7%). The 149 other functions have the exact same machine code as retail. All of them load a float constant:

- GCC puts float constants in an anonymous `.lit4` pool and loads them with an `R_MIPS_LITERAL` relocation (`.lit4+0x4000`, the `0x4000` being GCC's bias in the instruction).
- The retail asm loads the same constant with `%gp_rel(D_00392700)($gp)`, an `R_MIPS_GPREL16` relocation to a named symbol.

After linking, both sides land on the same address (that's why the ROM matches), but objdiff compares the objects before linking and sees two different relocations. Giving the retail target its own `.lit4` with the constant's bytes (what a per-TU `.lit4` subsegment in splat would do) doesn't change that: tested on TextFrmt, it stays at 16 / 17. Only a target rewritten to GCC's exact `R_MIPS_LITERAL` form matches, and that target wouldn't be the retail asm anymore.

Calls to `static` functions (`.text+offset`) and globals that have a C name but an address name in the retail asm don't count against the score: objdiff already matches those.

So `tools/buildtools/objdiff_normalize.py` makes a copy of each C object, only for objdiff, where those relocations take the retail form. It only touches a relocation when:

- the ROM checksum passed (it runs after it),
- the object is identical (code and relocations) to the one linked into the ROM,
- the function only differs from retail inside relocation fields.

Anything else is left as it is, so a real difference still shows up. The ROM and the real objects are never modified. The objdiff target stays the real retail asm.

## Which compiler?

I'm 100% sure the executable's game code was compiled with EE GCC 2.9. Now, what I'm not so sure of is which build of GCC2.9, the one I found the most success with was 991111-01, so I stuck with this one for the project (but I'm pretty sure this should be the right compiler!). 
The flags should be "-O2 -G8 -fno-strict-aliasing".
For a long time I thought the game used -ffast-math, but already deep inside the decomp I realized it didn't lol.
The UBISOUND.IRX module was compiled with IOP-GCC 2.8.1.
Also for some reason (????) there's a translation unit that was build with C++ instead: (GAM/ClockTimer_inv.cpp, Tmr/Timer.cpp), I don't get why but these were compiled with `cc1plus` from the same EE GCC 2.9.

## Build

See [docs/build.md](docs/build.md).

## Thanks

- the [Rayman 3 decompilation](https://github.com/LawlessCowboy/ray3) project for helping with matching some stuff quicker.
- the creators of all the amazing (human-made!) tools/ressources used here:
[splat](https://github.com/ethteck/splat),
[spimdisasm](https://github.com/Decompollaborate/spimdisasm),
[rabbitizer](https://github.com/Decompollaborate/rabbitizer),
[Slinky](https://github.com/decompals/slinky),
[objdiff](https://github.com/encounter/objdiff),
[decomp.me compilers](https://github.com/decompme/compilers),
[decomp-permuter](https://github.com/simonlindholm/decomp-permuter),
[Ninja](https://github.com/ninja-build/ninja)
- the [PCSX2](https://github.com/PCSX2/pcsx2) developers for helping a lot with the debugger.



