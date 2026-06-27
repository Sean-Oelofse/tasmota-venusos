# tasmota-venusos
Python integration for Tasmota that allows switch panel integration.


<img width="342" height="444" alt="image" src="https://github.com/user-attachments/assets/08aaab9e-6d9b-43e6-b113-251e101478b6" />
<img width="649" height="338" alt="image" src="https://github.com/user-attachments/assets/275f0fa4-e928-4263-93cf-2ee5bde5c01b" />



_________________________________________________________________________
Installation Instructions.
Root access is required see https://www.victronenergy.com/live/ccgx:root_access#root_access:
     
     ssh root@venus.local 
or 

     ssh root@192.xxx.xxx.xxx

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
__________________________________________________________________________
Enabling three state mode:
    
    cd /data && nano tasmota_config.json
Simply change three_state to true

<img width="285" height="701" alt="image" src="https://github.com/user-attachments/assets/a89218e7-6e2a-4ae8-9213-9a2c3d605f31" />

