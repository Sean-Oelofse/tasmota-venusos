# tasmota-venusos
Python integration for Tasmota that allows switch panel integration.


<img width="438" height="540" alt="image" src="https://github.com/user-attachments/assets/e35ea3f1-bfd4-4bc2-b525-076ef457cca3" />


_________________________________________________________________________
Installation Instructions.

Make sure both files install_tasmota_service.sh and tasmota.py are in the same directory e.g /data.
Then while in the same directory run ./install_tasmota_service.sh --mqtt-host 127.0.0.1 (you can change the ip if you have a different mqtt server).
__________________________________________________________________________
Useful commands:
    sv status /service/tasmota-discovery    # check status
    sv restart /service/tasmota-discovery   # restart
    sv stop /service/tasmota-discovery      # stop
    tail -f /var/log/tasmota-discovery/current  # live log

  To update the script after changes:
    cp tasmota.py /opt/victronenergy/tasmota-discovery/
    ./install_tasmota_service.sh --uninstall
