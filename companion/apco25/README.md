# APCO25

The Wavelens Scanner's decoder for trunked APCO Project 25 (P25) systems. It follows a system's control
channel and its voice grants, and decodes simulcast systems (several towers sending the same signal, as most
county systems do) with a CQPSK demodulator, where dsd-fme often hears calls start but decodes no voice.

## Install

On the Scanner (a Raspberry Pi with Raspberry Pi OS or Debian, GNU Radio 3.10):

    sudo /opt/wavelens/companion/pi/install-apco25.sh

or tick *Digital voice* on the Pi setup page, which builds it with dsd-fme. It builds from this folder into
`/opt/apco25`, takes a few minutes on a Pi 5, and needs internet for the apt packages. The bridge uses it for
trunked P25 from its next start; `WAVELENS_TRUNK_DECODER=dsd-fme` in `/etc/default/wavelens-bridge` goes back
to dsd-fme.

## Licence

GNU General Public License version 3: see LICENSE and NOTICE.md. Build notes from the original project are in
docs/upstream/.
