  // THIS IS THE CONTENT THAT MUST BE COPIED TO THE DEVICE THAT WILL MONITOR OTHER DEVICES

// EDIT THESE PARAMETERS

        // email of the person receiving the notification
        let email      = 'demonstration@allterco.com';

        // IP list of all devices that will be monitored
        let targetIp   = ['192.168.15.35'];

        // number in seconds that will take to check each device in the list
        let loopTime   = 60*60*12 ;

        // send emails even when the device is connected 
        let opt = false;

        // email server configuration. Two options. Comment the line that won't be used

        // 1) ip address of the computer that will run the email server locally
        // let computerIp = '192.168.15.99';
        
        // 2) Shelly public ip address of our server that is running the email server via internet 
        let computerIp = '23.115.145.128';


let obj = {
  customerEmail: email,
  plugIP: '',
  plugStatus: ''
};

  
Timer.set(loopTime*1000, true, function () {
  shellyStatus();
});
  
function sendEmail (obj) {

  let postData = { 
    url: 'http://' + computerIp + ':60222/email/outside/status/', 
    body: {
      customerEmail: obj.customerEmail, 
      plugIP: obj.plugIP, 
      plugStatus: obj.plugStatus
    },
    timeout: 100
  };
  
  Shelly.call(
    "HTTP.POST",
    postData,
    function (result, error_code, error_message) {
      // show what is being sent in the body to the email server
      //print('result:',JSON.stringify(result));
      // show any error code (0 = success)
      //print('error_code:',error_code);
      // show any error message
      //print('error_msg:',error_message);
    },
    null
  );
  
  //print(JSON.stringify(postData));
  
};

let ind = 0;
function shellyStatus(){
  if(ind < targetIp.length - 1){
    ind++;
  } else {
    ind = 0;
  };
 
  Shelly.call(
    "http.get",
    { "url": 'http://' + targetIp[ind] + '/rpc/Switch.GetStatus?id=0' },
    function (result, error_code, error_message, user_data) {
      if (error_code !== 0) {
        //print(ind, targetIp[ind], ": No power detected");
        obj.plugStatus = 'Disconnected from Power';
      } else {
        if (result.output) {
          //print(ind, targetIp[ind],': Connected but with problems');
          obj.plugStatus = 'Connected to power but we have detected problems with the energy output';
        } else {
          //print(ind, targetIp[ind],': All is well');
          obj.plugStatus = 'Connected to power';
        }
      }
      obj.plugIP = targetIp[ind];

      // sends email only in case there is an outage
      if(obj.plugStatus !== 'Connected to power' && opt === false){
        print('Send email when power off');
        sendEmail(obj);
      } else {
      // sends email in case there is power
        print('Send email when power on');
        sendEmail(obj);
      }
    },
    null
  );  
}