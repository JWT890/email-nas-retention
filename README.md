# email-nas-retention

When it comes to storage space allocation with emails and wanting to learn how to solve Gmail storage without mass deleting and wanting to save them, it is a good idea to build a NAS for it.

For this, it can be done with a Raspberry PI or OpenMediaVault in a VM  
OpenMediaVault download: https://www.openmediavault.org/download.html   
Download the stable version and go to VirtualBox

# VM Creation
In VirtualBox, click on new with these config:  
Name: OMV-NAS   
Type: Linux with Debian 64 Bit  
RAM MB: 4096    
VDI disk and dynamic    
Size of 60 GB   
Then set the network to bridged and attach the OMV iso. 

Then boot up the VM and go through the prompts that are straighforward and keep the domain blank.   
After waiting a few minutes:    
![alt text](image.png)  
Take note of the the address and go to a browser and type it to access OMV like so: 
![alt text](image-1.png)    
Then enter in the admin credentials to get in and see this: 
![alt text](image-2.png)    

# OMV
Then go the user icon and select change password for omv-nas to something more secure.  
Then go to storage and click on disks to see if the disk for the system shows up:   
![alt text](image-3.png)
Make sure before hand to create a second disk instead so that can be seen and like so:  
![alt text](image-4.png)    
Click on save and it will go the mount screen like below:   
![alt text](image-5.png)    
Then go to Storage -> Shared Folders and create the folder like so with similar input: 
![alt text](image-6.png)    
Hit save then hit configure and then go to Services -> SMB -> Shares and click on the dropdown in shares and see the shares creation and link the email-archive that was created and set to to guests-allowed. Then hit save and config.    
Then go the command line on host and write this command:    
![alt text](image-7.png)    
Since it doesn't show anything, that means it worked.   
Backtracking a little bit but for this screen:  
![alt text](image-8.png)    
Make sure the enabled at the very top is set it on or it will not work or error.    
Then go to the VM and login with root creds and type ls /srv/ and ip addr show to get the path and IP address of the OMV VM. Once so run chmod -R 777 /srv/dev-disk-by-uuid-c76de889-2fab-4dae-acae-c7f60e70078/email-archive.  
Then go to the Linux command host and run:  
![alt text](image-9.png)    
When it says Share OK, the NAS is now mounted and time to move on to the next.  

# Setup
In the command line type mkdir ~/mail-retention then cd ~/mail-retention.   
Then run sudo apt install python3-pip, then run pip install pyyaml python-dotenv.   
Then type nano ~/mail-retention/.env which will open up a empty file for the gmail, app password and path for the nas. For the Gmail App password go to myaccount.google.com after enabling 2 factor and type in myaccount.google.com/apppasswords and click on create and name it something and copy and save the password generated.  
