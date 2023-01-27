import { Router } from "express";
import { sendOutsideEmail } from "./email.services.js";

export const emailRouter = Router();

emailRouter.post("/email/outside/status/", async (req, res) => {
    let obj ={}

    if(JSON.stringify(req.body).length > 2) {
        // console.log(1, req.body)
        obj = req.body
    } else {
        // console.log(2, req.query.obj) 
        obj = req.query.obj
    }
    try {
        const ret = await sendOutsideEmail(obj);
        res.status(200).send('Email sent. ' + JSON.parse(ret));
    } catch (err) {
        res.status(400).send('Error sending email. ' + err);
    }
});

emailRouter.get("/testconnection", async(req,res) => {
    try{
        res.status(200).send('Connected');
    } catch (err) {
        res.status(400).send(err);
    }
})