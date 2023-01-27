import express from "express";
import cors from "cors";

import { emailRouter } from "./email.routes.js";

const app = express();
app.use(express.json());
app.use(cors());

// using the routes that we created in the routes folder
app.use(emailRouter);

// for local test only without firebase
const PORT = 80;
app.listen(PORT, () => {
  // console.log(`http://localhost:${PORT}`);
  console.log(`Server is ready to send emails`);
});
