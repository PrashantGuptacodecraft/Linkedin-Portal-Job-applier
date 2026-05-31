from pathlib import Path
from models import CandidateProfile
import resume_generator

out = Path('data/uploads/test_generated_from_template.pdf')
out.parent.mkdir(parents=True, exist_ok=True)

cand = CandidateProfile(
    name='Prashant Gupta',
    email='adityagupta983869@gmail.com',
    phone='+91-9838693305',
    location='Ghaziabad, India',
    target_role='Java Developer + C2C',
    technical_skills='Java, Spring Boot, Hibernate/JPA, REST APIs, MySQL',
    projects='Employee Management System\nOnline Banking System\nJob Portal Backend API'
)

path = resume_generator.generate_resume(cand, str(out))
print('Generated:', path)
print('Size:', out.stat().st_size)
