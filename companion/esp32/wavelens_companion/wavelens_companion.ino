// Wavelens companion — ESP32 firmware. The code is in wavelens_companion.cpp, next to this file.
//
// This file is here because the Arduino IDE wants a sketch named after the folder; it compiles every
// .cpp in the folder alongside it. PlatformIO (platformio.ini here) compiles the .cpp directly. Keeping
// the code in a .cpp means neither tool rewrites it: the IDE's and PlatformIO's .ino-to-C++ conversion
// inserts function prototypes above the file's own type declarations, which breaks on the SA818 and
// spectrum structs (undefined references to setup()/loop(), "Sa818 was not declared").
