#include "common/yaml/Yaml.h"
#include <iostream>

// Use the same parser as the installed DVM executables, without opening RF,
// serial ports or network sockets. PyYAML acceptance alone is insufficient.
int main(int argc, char** argv)
{
    if (argc < 2) return 2;
    for (int i = 1; i < argc; ++i) {
        yaml::Node root;
        if (!yaml::Parse(root, argv[i])) {
            std::cerr << "DVM parser rejected " << argv[i] << '\n';
            return 1;
        }
    }
    return 0;
}
