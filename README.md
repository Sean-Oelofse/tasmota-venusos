# tasmota-venusos
Python integration for Tasmota that allows switch panel integration.


<img width="1000" height="700" alt="image" src="https://github.com/user-attachments/assets/56004b4a-02ca-4d00-a802-b3c2ad52b24c" />



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
Switch type (toggle / three-state / momentary):

Pick it in the GUI - Settings -> the switch -> Type - and it is saved for
you. Or edit the config by hand:

    cd /data && nano tasmota_config.json

    "three_state": true     Off / On / Auto
    "momentary":   true     push button
                            (neither set = a plain toggle)

three_state takes priority if both are set.
<img width="1184" height="785" alt="image" src="https://github.com/user-attachments/assets/3d6667f2-38fa-4b59-add9-e986b5802c5c" />

__________________________________________________________________________
Momentary push buttons (gates, garage doors, doorbells):

    "gate": {
      "momentary": true,
      "pulse_ms": 600
    }

Each press closes the relay for pulse_ms milliseconds and then releases it
again, instead of latching on. pulse_ms is optional and defaults to 600,
which is also the minimum - anything lower is raised to 600 so the contact
is long enough for gate and garage-door controllers to see.

Holding the button down does not extend the pulse; pressing again while one
is running does. A relay that was left on is released as soon as the channel
becomes momentary, so a button never sits there energised.


