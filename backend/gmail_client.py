import smtplib
from email.message import EmailMessage
from typing import Optional
from loguru import logger
import google.generativeai as genai
import os
import mimetypes
import re

from .models import CandidateProfile, LoginCredentials

async def send_c2c_application_email(
    to_email: str, 
    post_text: str, 
    candidate: CandidateProfile, 
    resume_path: Optional[str], 
    credentials: LoginCredentials,
    gemini_api_key: str
) -> bool:
    """Sends a personalized email application to a C2C recruiter using Gemini for the cover letter."""
    
    # Validate credentials
    if not credentials.gmail_address or not credentials.gmail_app_password:
        logger.error("Cannot send email: gmail_address or gmail_app_password missing in credentials.")
        return False
    
    # Validate API key
    if not gemini_api_key or not gemini_api_key.strip():
        logger.error("Cannot send email: GEMINI_API_KEY is not configured.")
        return False
    
    # Validate email format
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', to_email):
        logger.error(f"Invalid email format: {to_email}")
        return False
        
    try:
        # Generate personalized email body
        from .config import get_current_gemini_key
        genai.configure(api_key=get_current_gemini_key())
        model = genai.GenerativeModel("gemini-2.0-flash")
        
        prompt = f"""
        You are an AI assistant writing a short, professional email application for a Corp-to-Corp (C2C) or contract role.
        
        Candidate Name: {candidate.name}
        Candidate Role: {candidate.target_role}
        Years of Experience: {candidate.years_of_experience}
        Location: {candidate.preferred_location}
        Visa/Work Auth: {candidate.work_authorization}
        
        Recruiter's LinkedIn Post:
        "{post_text}"
        
        Write the email body ONLY. Keep it concise (under 4 sentences). Mention the candidate's core skills, their visa status if applicable, and state that the resume is attached. 
        Format it formally but warmly.
        Do not include the Subject line in the output, just the body.
        """
        response = model.generate_content(prompt)
        email_body = response.text.strip()
        
        # Build subject
        subject = f"Application for Role: {candidate.target_role} - {candidate.name}"
        
        # Construct email
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = credentials.gmail_address
        msg['To'] = to_email
        msg.set_content(email_body)
        
        # Attach resume
        if resume_path:
            if not os.path.exists(resume_path):
                logger.warning(f"Resume file not found: {resume_path} - sending email without attachment")
            else:
                try:
                    ctype, encoding = mimetypes.guess_type(resume_path)
                    if ctype is None or encoding is not None:
                        ctype = 'application/octet-stream'
                    maintype, subtype = ctype.split('/', 1)
                    
                    with open(resume_path, 'rb') as f:
                        msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(resume_path))
                except Exception as attach_err:
                    logger.warning(f"Failed to attach resume: {str(attach_err)}")
        else:
            logger.warning("No resume path provided - sending email without attachment")
            
        # Send email via SMTP
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                smtp.login(credentials.gmail_address, credentials.gmail_app_password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as auth_err:
            logger.error(f"Gmail authentication failed: {str(auth_err)}")
            return False
        except smtplib.SMTPException as smtp_err:
            logger.error(f"SMTP error while sending email: {str(smtp_err)}")
            return False
            
        logger.info(f"Successfully sent C2C application to {to_email}")
        return True
        
    except ValueError as val_err:
        logger.error(f"Invalid input for email to {to_email}: {str(val_err)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending email to {to_email}: {type(e).__name__}: {str(e)}")
        return False
