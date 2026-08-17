# SNSPD Multi-Channel Biasing Current Source

#### Objective:

SMU is used for SNSPD to:

 - Determine switching current (Typically around 3μA ) by hysteresis V-I sweep measurement
 - Source steady biasing current to the nano-wire

However, the commercial SMU are typically single channel, and large sweep range is unnecessary for SNSPD.

This PCB provides necessary features for SNSPD biasing with 4 channels at around 300CAD per assembled PCB.

#### Features:

- 4 channels independently controlled from Python Remi GUI
- 10nA measurement precision
- Hysteresis V-I sweep with controllable range (maximum ± 1.0 V) and step size
    - With 100 k Ω resistor down steam, current will be around 10μA at 1.0 V
    - Measurements plotted on GUI and also recorded as an txt file
- Steady biasing current sourcing at set voltage
- Per-channel current sense calibration
- Per-channel ODR settings 

#### Require:

- Assembled PCB
- 9V / 1A Power Adapter (Current output higher than 1A is also fine)
- Micro USB cable
- Computer
- 100kΩ Resistor
- Electrometer (Only for initial calibration)

#### Contributors
Firmware developed by [Taichi Kamei](https://github.com/Taichi-Kamei) and PCB designed by [Avi Guha](https://github.com/avi-guha), with contributions by: [Mateo Branion-Calles](https://github.com/MateoBCalles), Adan Azem

# Installation Guide
Requires installation of uv Python package and project manager

- **[GUI installation & launch](https://github.com/SiEPIC/SNSPD_multichannel_CurrentBias/wiki/GUI-Installation)**

- **[Flashing Firmware](https://github.com/SiEPIC/SNSPD_multichannel_CurrentBias/wiki/Firmware-Installation)**

# GUI Instruction

- **[GUI Guide](https://github.com/SiEPIC/SNSPD_multichannel_CurrentBias/wiki/GUI-Guide)**


# Screenshots

![PCB](./Documentation/images/pcb.jpg)
