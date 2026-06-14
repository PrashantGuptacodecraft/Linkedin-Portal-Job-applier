import smtplib
from email.message import EmailMessage
from typing import Optional
from loguru import logger
import google.generativeai as genai
import os
import mimetypes

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
    
    if not credentials.gmail_address or not credentials.gmail_app_password:
        logger.error("Cannot send email: gmail_address or gmail_app_password missing in credentials.")
        return False
        
    try:
        # Generate personalized email body
        from .config import get_current_gemini_key
        genai.configure(api_key=get_current_gemini_key())
        model = genai.GenerativeModel("gemini-2.0-flash")
        
        prompt = f"""
        You are an AI assistant writing a short, professional email application for a Corp-to-Corp (C2C) or contract role.
        
        Candidate Name: {candidate.first_name} {candidate.last_name}
        Candidate Role: {candidate.job_title}
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
        subject = f"Application for Role: {candidate.job_title} - {candidate.first_name} {candidate.last_name}"
        
        # Construct email
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = credentials.gmail_address
        msg['To'] = to_email
        msg.set_content(email_body)
        
        # Attach resume
        if resume_path and os.path.exists(resume_path):
            ctype, encoding = mimetypes.guess_type(resume_path)
            if ctype is None or encoding is not None:
                ctype = 'application/octet-stream'
            maintype, subtype = ctype.split('/', 1)
            
            with open(resume_path, 'rb') as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(resume_path))
        else:
            logger.warning(f"Resume path not found: {resume_path}")
            
        # Send email via SMTP
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(credentials.gmail_address, credentials.gmail_app_password)
            smtp.send_message(msg)
            
        logger.info(f"Successfully sent C2C application to {to_email}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {str(e)}")
        return False
