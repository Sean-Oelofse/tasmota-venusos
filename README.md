# tasmota-venusos
Python integration for Tasmota that allows switch panel integration.


<img width="338" height="440" alt="image" src="https://github.com/user-attachments/assets/e35ea3f1-bfd4-4bc2-b525-076ef457cca3" /> <img width="649" height="338" alt="image" src="https://github.com/user-attachments/assets/275f0fa4-e928-4263-93cf-2ee5bde5c01b" />



_________________________________________________________________________
Installation Instructions.

Make sure both files install_tasmota_service.sh and tasmota.py are in the same directory e.g /data.
Then while in the same directory run:
     
     ./install_tasmota_service.sh --mqtt-host 127.0.0.1
--mqtt-host can be changed to match your current mqtt broker 
Or if you would like a simple way to install run:
    
    bash <(curl -fsSL https://raw.githubusercontent.com/Sean-Oelofse/tasmota-venusos/main/install_tasmota_service.sh) --mqtt-host 127.0.0.1
__________________________________________________________________________
Useful commands:

    svstat /service/tasmota-discovery    # check status
    
    svc -t /service/tasmota-discovery   # restart

  To update the script after changes:
    
    cp tasmota.py /opt/victronenergy/tasmota-discovery/
    
    ./install_tasmota_service.sh --uninstall
