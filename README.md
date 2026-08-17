

# SNSPD Multi-Channel Biasing Current Source

#### Features:

- Hysteresis V-I sweep with controllable range (maximum ± 1.0 V )
    - With 100 k Ω resistor down steam, current will be around 10 μ A at 1.0 V
    - Measurements plotted on GUI and also saved as an txt file
- Steady biasing current sourcing at set voltage
    - Plot on calibration tab for measurement comparison with electrometer
- 4 channels independently controlled via GUI
- 10nA measurement precision
- Per-channel ODR settings 
- Per-channel current sense Calibration

#### Requires:

- Assembled PCB
- 9V / 1A Power Adapter (Current output higher than 1A is also fine)
- Micro USB cable
- Computer
- 100 k Ω Resistor
- Electrometer (Only for initial calibration)

Firmware developed by [Taichi Kamei](https://github.com/Taichi-Kamei) and PCB designed by [Avi Guha](https://github.com/avi-guha), with contributions by: [Mateo Branion-Calles](https://github.com/MateoBCalles), Adan Azem

# Installation Guide

- **[GUI installation & launch](https://github.com/SiEPIC/SNSPD_multichannel_CurrentBias/wiki/GUI-Installation)**

- **[Firmware flash](https://github.com/SiEPIC/SNSPD_multichannel_CurrentBias/wiki/Firmware-Installation)**

# GUI Instruction


## Objective:

SMU is used for SNSPD to:

 - Determine switching current (Typically around 3 μ A ) by hysteresis V-I sweep measurement
 - Source steady biasing current to the nano-wire

However, the commercial SMU are typically single channel, and large sweep range it offers is unnecessary to source what SNSPD needs.

This PCB provides necessary features for SNSPD biasing with 4 channels at around 300CAD per assembled PCB.

# Screenshots

![PCB](./Documentation/images/pcb.jpg)
