import json
import logging
from aiohttp import web

logger = logging.getLogger(__name__)

async def handle_login(request):
    html = """
    <html>
      <head><title>LinkedIn Login</title></head>
      <body>
        <form action="/login-submit" method="post">
          <input type="text" id="username" name="username" />
          <input type="password" id="password" name="password" />
          <button type="submit" data-litms-control-urn="login-submit">Sign in</button>
        </form>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_login_submit(request):
    return web.HTTPFound('/feed')

async def handle_feed(request):
    html = """
    <html>
      <head><title>LinkedIn Feed</title></head>
      <body><h1>Welcome to your feed!</h1></body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_search(request):
    port = request.app['port']
    base_url = f"http://127.0.0.1:{port}"
    html = f"""
    <html>
      <head><title>LinkedIn Jobs</title></head>
      <body>
        <ul>
          <li class="scaffold-layout__list-item" data-job-id="1001">
            <a href="{base_url}/jobs/view/1001/">Software Engineer (Greenhouse)</a>
          </li>
          <li class="scaffold-layout__list-item" data-job-id="1002">
            <a href="{base_url}/jobs/view/1002/">Data Scientist (Lever)</a>
          </li>
          <li class="scaffold-layout__list-item" data-job-id="1003">
            <a href="{base_url}/jobs/view/1003/">Product Manager (Workday)</a>
          </li>
          <li class="scaffold-layout__list-item" data-job-id="1004">
            <a href="{base_url}/jobs/view/1004/">Designer (Captcha)</a>
          </li>
          <li class="scaffold-layout__list-item" data-job-id="1005">
            <a href="{base_url}/jobs/view/1005/">QA Engineer (OTP)</a>
          </li>
        </ul>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_job_view(request):
    job_id = request.match_info.get('id')
    port = request.app['port']
    base_url = f"http://127.0.0.1:{port}"
    target = "_blank"
    
    if job_id == '1001':
        apply_url = f"{base_url}/greenhouse/job/1001"
        title = "Software Engineer"
        company = "Tech Corp"
    elif job_id == '1002':
        apply_url = f"{base_url}/lever/job/1002"
        title = "Data Scientist"
        company = "Data Inc"
    elif job_id == '1003':
        apply_url = f"{base_url}/workday/job/1003"
        title = "Product Manager"
        company = "Workday Corp"
    elif job_id == '1004':
        apply_url = f"{base_url}/captcha/job/1004"
        title = "Designer"
        company = "Captcha Inc"
    elif job_id == '1005':
        apply_url = f"{base_url}/otp/job/1005"
        title = "QA Engineer"
        company = "OTP Inc"
    elif job_id == '1006':
        apply_url = f"{base_url}/greenhouse/job/1006"
        title = "Backend Engineer"
        company = "Tech Corp"
        target = "_self"
    else:
        return web.Response(status=404)

    html = f"""
    <html>
      <head><title>{title} | LinkedIn</title></head>
      <body>
        <div class="jobs-unified-top-card__job-title"><h1>{title}</h1></div>
        <div class="jobs-unified-top-card__company-name"><a href="#">{company}</a></div>
        <div class="jobs-unified-top-card__primary-description">New York, NY</div>
        <div class="jobs-description__content">We are looking for a {title}...</div>
        <a class="jobs-apply-button" href="{apply_url}" target="{target}">Apply on company website</a>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_greenhouse(request):
    html = """
    <html>
      <head><title>Apply to Tech Corp - Greenhouse</title></head>
      <body>
        <form action="/greenhouse/submit" method="post">
          <label for="first_name">First Name</label><input type="text" id="first_name" name="first_name" />
          <label for="last_name">Last Name</label><input type="text" id="last_name" name="last_name" />
          <label for="email">Email</label><input type="text" id="email" name="email" />
          <label for="phone">Phone</label><input type="text" id="phone" name="phone" />
          <label for="resume">Resume</label><input type="file" id="resume" name="resume" />
          
          <div class="custom-question">
            <label>Why do you want to work here? <span class="asterisk">*</span></label>
            <input type="text" name="custom_question_1" required />
          </div>
          
          <button type="submit" id="submit_app">Submit Application</button>
        </form>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_lever(request):
    html = """
    <html>
      <head><title>Apply to Data Inc - Lever</title></head>
      <body>
        <form action="/lever/submit" method="post">
          <label for="name">Full Name</label><input type="text" id="name" name="name" />
          <label for="email">Email</label><input type="text" id="email" name="email" />
          <label for="phone">Phone</label><input type="text" id="phone" name="phone" />
          <label for="resume">Resume/CV</label><input type="file" id="resume" name="resume" />
          <label for="cover_letter">Cover Letter</label><textarea id="cover_letter" name="cover_letter"></textarea>
          
          <button type="submit" class="postings-btn template-btn-submit">Submit Application</button>
        </form>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_workday(request):
    # Simulate a login wall that needs to be passed before seeing the form
    cookie = request.cookies.get('workday_logged_in')
    if not cookie:
        html = """
        <html>
          <head><title>Workday Login</title></head>
          <body>
            <h2>Sign In</h2>
            <form action="/workday/login" method="post">
              <input type="text" name="username" />
              <input type="password" name="password" />
              <button type="submit" data-automation-id="signInSubmitButton">Sign In</button>
            </form>
          </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
        
    html = """
    <html>
      <head><title>Workday Apply</title></head>
      <body>
        <form action="/workday/submit" method="post">
          <input type="text" name="firstName" data-automation-id="legalNameSection_firstName" />
          <input type="text" name="lastName" data-automation-id="legalNameSection_lastName" />
          <button type="submit" data-automation-id="bottom-navigation-next-button">Submit</button>
        </form>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_workday_login(request):
    resp = web.HTTPFound('/workday/job/1003')
    resp.set_cookie('workday_logged_in', 'true')
    return resp

async def handle_captcha(request):
    cookie = request.cookies.get('captcha_solved')
    if not cookie:
        html = """
        <html>
          <head><title>Just a moment...</title></head>
          <body>
            <div class="h-captcha">Please solve captcha</div>
            <a href="/captcha/solve">Click here to mock solve</a>
          </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
        
    html = """
    <html>
      <head><title>Captcha Passed</title></head>
      <body>
        <form action="/captcha/submit" method="post">
          <input type="text" name="name" />
          <button type="submit">Submit</button>
        </form>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_captcha_solve(request):
    resp = web.HTTPFound('/captcha/job/1004')
    resp.set_cookie('captcha_solved', 'true')
    return resp

async def handle_otp(request):
    cookie = request.cookies.get('otp_solved')
    if not cookie:
        html = """
        <html>
          <head><title>Enter OTP</title></head>
          <body>
            <h2>Verify your identity</h2>
            <input type="text" id="otp-code" placeholder="Enter code sent to email" />
            <button id="verify-button">Verify</button>
            <a href="/otp/solve">Click here to mock solve</a>
          </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
        
    html = """
    <html>
      <head><title>OTP Passed</title></head>
      <body>
        <form action="/otp/submit" method="post">
          <input type="text" name="name" />
          <button type="submit">Submit</button>
        </form>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def handle_otp_solve(request):
    resp = web.HTTPFound('/otp/job/1005')
    resp.set_cookie('otp_solved', 'true')
    return resp

async def handle_submit(request):
    html = """
    <html>
      <head><title>Application Submitted</title></head>
      <body>
        <h1>Application submitted successfully</h1>
      </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

def build_app():
    app = web.Application()
    app.add_routes([
        web.get('/login', handle_login),
        web.post('/login-submit', handle_login_submit),
        web.get('/feed', handle_feed),
        web.get('/search', handle_search),
        web.get('/jobs/view/{id}/', handle_job_view),
        
        web.get('/greenhouse/job/1001', handle_greenhouse),
        web.post('/greenhouse/submit', handle_submit),
        
        web.get('/lever/job/1002', handle_lever),
        web.post('/lever/submit', handle_submit),
        
        web.get('/workday/job/1003', handle_workday),
        web.post('/workday/login', handle_workday_login),
        web.post('/workday/submit', handle_submit),
        
        web.get('/captcha/job/1004', handle_captcha),
        web.get('/captcha/solve', handle_captcha_solve),
        web.post('/captcha/submit', handle_submit),
        
        web.get('/otp/job/1005', handle_otp),
        web.get('/otp/solve', handle_otp_solve),
        web.post('/otp/submit', handle_submit),
    ])
    return app

class MockServer:
    def __init__(self):
        self.app = build_app()
        self.runner = None
        self.site = None
        self.port = None

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        
        # Get dynamic port
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.app['port'] = self.port
        logger.info(f"Mock server started on http://127.0.0.1:{self.port}")
        return self.port

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

async def run_server_forever():
    server = MockServer()
    port = await server.start()
    print(f"Server running on port {port}")
    import asyncio
    await asyncio.Event().wait()

if __name__ == '__main__':
    import asyncio
    asyncio.run(run_server_forever())
