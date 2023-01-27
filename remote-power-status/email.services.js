import nodemailer from "nodemailer";
import { CREDENTIALS } from "./secrets.js";

export const sendOutsideEmail = async (obj) => {

  const transporter = nodemailer.createTransport({
    service : CREDENTIALS.EMAIL_SERVICE,
    auth : {
        user : CREDENTIALS.EMAIL_USERNAME,
        pass : CREDENTIALS.EMAIL_PASSWORD
    }
  });

  const htmlCode = `<p>The plug IP <strong>${obj.plugIP}</strong> 
                is currently <strong>${obj.plugStatus}</strong></p>`

  const message = {
      from: CREDENTIALS.EMAIL_FROM,
      to: obj.customerEmail,
      subject: 'Shelly Script Status',
      html: htmlCode
    };


  // console.log('Sending email...', htmlCode, message)
  const response = await transporter.sendMail(message)
  return response

}