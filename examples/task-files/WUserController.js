import bcrypt from "bcrypt";
import nodemailer from 'nodemailer';
import httpStatusCodes from "http-status-codes";
import jwt from "jsonwebtoken";
import { Op } from "sequelize"; // For filtering users
import axios from 'axios';

// Import your Sequelize models
import User from "../models/userModel.js";
import {oauth2Client} from "../utils/oauth2client.js";
import { ApiError } from "../utils/ApiErrors.js";
import { generateRandomCode, generateRandomToken } from "../utils/methods.js";
import ApiResponse from '../utils/ApiResponse.js';

// User registration
const userRegisteration = async (req, res, next) => {
  const { firstName, lastName, email, phone, password,role } = req.body;

  const isUserExist = await User.findOne({ where: { email: email } });
  if (isUserExist) {
    throw new ApiError(
      httpStatusCodes.BAD_REQUEST,
      "User already exists",
      httpStatusCodes.BAD_REQUEST
    );
  }

  const salt = await bcrypt.genSalt(10);
  const hashedPassword = await bcrypt.hash(password, salt);
  const confirmationToken = generateRandomCode();

  const userData = await User.create({
    firstName,
    lastName,
    email,
    phone,
    role,
    password: hashedPassword,
    token: confirmationToken
  });


const loginUser = async (req, res) => {
  const { email, password } = req.body;

  if (!email || !password) {
    throw new ApiError(
      httpStatusCodes.BAD_REQUEST,
      "All fields are required",
      httpStatusCodes.BAD_REQUEST
    );
  }

  const user = await User.findOne({ where: { email } });
  if (!user) {
    throw new ApiError(
      httpStatusCodes.BAD_REQUEST,
      "Invalid email or password",
      httpStatusCodes.BAD_REQUEST
    );
  }

  if (await bcrypt.compare(password, user.password)) {
    if (user.token) {
      throw new ApiError(
        httpStatusCodes.UNAUTHORIZED,
        "Email verification is pending",
        httpStatusCodes.UNAUTHORIZED
      );
    }

    const accessToken = jwt.sign(
      {
        user: {
          id: user.id,
          role:user.role
        },
      },
      process.env.JWT_KEY,
      { expiresIn: "1d" }
    );
    ApiResponse.result(res, { status: "Login Successful", token: accessToken }, httpStatusCodes.OK);
  } else {
    throw new ApiError(
      httpStatusCodes.BAD_REQUEST,
      "Email or password is not valid",
      httpStatusCodes.BAD_REQUEST
    );
  }
};

// Delete user
const deleteUser = async (req, res, next) => {
  const id = req.query.id;

  await User.destroy({ where: { id } });
  ApiResponse.result(res, { status: "User deleted successfully" }, httpStatusCodes.OK);
};

// Current user
const currentUser = async (req, res, next) => {
  const user = await User.findByPk(req.user.id, {
    attributes: { exclude: ['password'] }
  });

  ApiResponse.result(res, { user }, httpStatusCodes.OK);
};

const updateUser = async (req, res, next) => {
  const { firstName, lastName, phone } = req.body;

  const updateFields = {};
  if (firstName !== undefined) updateFields.firstName = firstName;
  if (lastName !== undefined) updateFields.lastName = lastName;
  if (phone !== undefined) updateFields.phone = phone;

  await User.update(updateFields, { where: { id: req.user.id } });

  ApiResponse.result(res, { status: 'User updated successfully' }, httpStatusCodes.OK);
};

const changePassword = async (req, res, next) => {
  const { oldpassword, newpassword } = req.body;

  if (!oldpassword || !newpassword) {
    throw new ApiError(
      httpStatusCodes.BAD_REQUEST,
      "Old password or new password is not provided",
      httpStatusCodes.BAD_REQUEST
    );
  }

  const user = await User.findByPk(req.user.id);
  if (user && await bcrypt.compare(oldpassword, user.password)) {
    const salt = await bcrypt.genSalt(10);
    const hashedPassword = await bcrypt.hash(newpassword, salt);

    await user.update({ password: hashedPassword });

    ApiResponse.result(res, { message: "Password changed successfully" }, httpStatusCodes.OK);
  } else {
    throw new ApiError(
      httpStatusCodes.FORBIDDEN,
      "Invalid old password",
      httpStatusCodes.FORBIDDEN
    );
  }
};

const forgetPassword = async (req, res, next) => {
  const { email } = req.body;
  if (!email) {
    throw new ApiError(
      httpStatusCodes.BAD_REQUEST,
      "Email is required",
      httpStatusCodes.BAD_REQUEST
    );
  }

  const user = await User.findOne({ where: { email } });
  if (user) {
    const randomToken = generateRandomToken(24);
    await user.update({ resetToken: randomToken });

    const transporter = nodemailer.createTransport({
      service: 'gmail',
      auth: {
        user: process.env.SIGNUP_EMAIL,
        pass: process.env.SIGNUP_EMAIL_PASS,
      },
    });

    const mailOptions = {
      from: process.env.SIGNUP_EMAIL,
      to: email,
      subject: 'Password Reset Request',
      html: `
        <h2>Password Reset</h2>
        <p>Hello,</p>
        <br />
        <p>You recently requested a password reset for your account. Click the link below to reset your password:</p>
        <p><a href=${process.env.FRONT_END_URL + '/auth/reset-password/' + randomToken} target="_blank">Reset Password</a></p>
        <p>If you did not request a password reset, please ignore this email.</p>
        <br />
        <p>Best regards,<br>AutoTrak Team</p>
      `
    };

    transporter.sendMail(mailOptions);
    ApiResponse.result(res, { message: "Email sent successfully." }, httpStatusCodes.OK);
  } else {
    throw new ApiError(
      httpStatusCodes.FORBIDDEN,
      "Invalid Email",
      httpStatusCodes.FORBIDDEN
    );
  }
};

const verifyPasswordResetToken = async (req, res) => {
  const { password, token } = req.body;

  if (!token) {
    throw new ApiError(
      httpStatusCodes.UNPROCESSABLE_ENTITY,
      "Token is required",
      httpStatusCodes.UNPROCESSABLE_ENTITY
    );
  }

  const user = await User.findOne({ where: { resetToken: token } });

  if (!user) {
    throw new ApiError(
      httpStatusCodes.UNPROCESSABLE_ENTITY,
      "Token is invalid",
      httpStatusCodes.UNPROCESSABLE_ENTITY
    );
  }

  const hash = await bcrypt.hash(password, 10);
  await user.update({ password: hash, resetToken: null });

  ApiResponse.result(res, { status: 'Password reset successful' }, httpStatusCodes.OK);
};


const listMembers = async (req, res) => {
  const { page = 1, limit = 10 } = req.query;

  const offset = (page - 1) * limit;

  // Fetch users with pagination
  const { count, rows: users } = await User.findAndCountAll({
    where: {
      role: { [Op.ne]: "Admin" }, // Exclude admin accounts
    },
    attributes: ["firstName", "lastName", "email", "phone"],
    limit: parseInt(limit),
    offset: parseInt(offset),
  });

  const totalPages = Math.ceil(count / limit);

  ApiResponse.result(
    res,
    {
      members: users,
      meta: {
        totalMembers: count,
        currentPage: page,
        totalPages,
      },
    },
    httpStatusCodes.OK
  );
};
const googleAuth = async (req, res) => {
  try{
  const { code } = req.body;
  console.log({code})
  const googleRes = await oauth2Client.getToken(code);
  const tokens = googleRes.tokens;
  console.log("Tokens received:", tokens);

  if (!tokens.access_token) {
    return res.status(401).json({ error: "Failed to obtain access token" });
  }

  // Set credentials for further API calls
  oauth2Client.setCredentials(tokens);
  const userRes = await axios.get(
    `https://www.googleapis.com/oauth2/v1/userinfo?alt=json&access_token=${tokens.access_token}`
  );

  console.log({userRes})
 
  

  let user = await User.findOne({where:{ email: userRes?.data?.email }});
  
  if (user) {
    if (user.dataValues.token!=null) {
      throw new ApiError(
        httpStatusCodes.UNAUTHORIZED,
        "Email verification is pending",
        httpStatusCodes.UNAUTHORIZED
      );
    }
    const accessToken = jwt.sign(
      {
        user: {
          id: user.id,
          role:user.role,
        },
      },
      process.env.JWT_KEY,
      { expiresIn: "30d" }
    );
    res.cookie("accessToken", accessToken, {
      HttpOnly: true,
      //TODO: will turn it to true once ssl is configured
      secure: false,
      maxAge: 24 * 60 * 60 * 1000,
      sameSite: "none",
    });
    ApiResponse.result(res,  accessToken, httpStatusCodes.OK);
  } else {
    console.log("started here")
    user = await User.create({
      email: userRes?.data?.email,
      firstName: userRes?.data?.given_name,
      lastName: userRes?.data?.family_name,
      password: null 
      // Other user fields can be added here as necessary
    });

    // Step 4: Save the new user to the database
    

    // Step 5: Generate a JWT token for the new user
    const accessToken = jwt.sign(
      {
        user: {
          id: user.id,
          role:user.role,
        },
      },
      process.env.JWT_KEY,
      { expiresIn: "30d" }
    );

    // Step 6: Set the JWT token as a cookie
    res.cookie("accessToken", accessToken, {
      httpOnly: true,
      secure: false,  // Change to `true` when SSL is configured
      maxAge: 24 * 60 * 60 * 1000,  // 1 day
      sameSite: "none",  // Required for cross-origin cookies
    });

    // Step 7: Send a successful sign-up response
    ApiResponse.result(res, accessToken, httpStatusCodes.CREATED);
  }
}catch(error){
  console.log({error})
  ApiResponse.result(res,  {error} );
}
};

export {
  userRegisteration,
  loginUser,
  currentUser,
  deleteUser,
  validateConfirmationToken,
  updateUser,
  changePassword,
  forgetPassword,
  verifyPasswordResetToken,
  googleAuth,
  listMembers,

};
