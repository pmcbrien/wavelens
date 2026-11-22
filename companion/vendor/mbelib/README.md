PATENT NOTICE

```
This source code is provided for educational purposes only.  It 
is a written description of how certain voice encoding/decoding
algorithms could be implemented.  Executable objects compiled or
derived from this package may be covered by one or more patents.
Readers are strongly advised to check for any patent restrictions 
or licencing requirements before compiling or using this source code.
```

mbelib 1.3.4 w/ AMBE and AMBE+2 Tone Synthesis

```
mbelib supports the 7200x4400 bit/s codec used in P25 Phase 1,
the 7100x4400 bit/s codec used in ProVoice, AMBE used in DSTAR,
and AMBE+2 used in DMR, NXDN, P25 Phase 2, dPMR, etc.
```

Build Instructions

```
git clone https://github.com/lwvmobile/mbelib.git
git checkout ambe_tones
cd mbelib
mkdir build
cd build
cmake ..
make
sudo make install
```

To Disable AMBE and AMBE+2 Tones, run cmake with option `cmake -DNOTONES=ON ..` when building.