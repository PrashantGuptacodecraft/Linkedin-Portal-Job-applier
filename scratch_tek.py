import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        url = "https://apply.teksystems.com/v1/s/?opco=TEK&params=cbRdPXv3VUjRWueGqdvckt8SSNw%2B%2FXK%2FLMXamyL0IRn3eHp9Sn54XfmSqrAufELyW3ECOXDsgo3Ea4%2FsYpG4KidPzVEA4PZIOphF84oPlXk5q8MXwH2uTzsmWRQx5IW7&s_id=4106&jdg=false&icid=linkedin_recruitics&rx_campaign=Linkedin1&rx_ch=connector&rx_group=123600&rx_id=0c46611a-5eb0-11f1-aef1-efd3c8f375aa&rx_job=JP-006066006&rx_medium=post&rx_r=none&rx_source=Linkedin&rx_vp=slots&rx_viewer=5b848f945f1211f1ab60f12eeffe9b10874b543552ca4d46bb119cba4226f980&ecid=undefined"
        print("Navigating...")
        await page.goto(url)
        await page.wait_for_load_state("networkidle", timeout=15000)
        
        # find file inputs
        inputs = await page.query_selector_all("input[type='file']")
        print(f"Found {len(inputs)} file inputs.")
        if len(inputs) > 0:
            inp = inputs[0]
            try:
                # Need absolute path
                abs_path = os.path.abspath("test_resume.pdf")
                print(f"Uploading {abs_path} ...")
                await inp.set_input_files(abs_path)
                print("Uploaded via set_input_files.")
                
                # wait a bit and check if UI shows the file name
                await asyncio.sleep(3)
                html = await page.content()
                if "test_resume" in html:
                    print("UI registered the file upload successfully!")
                else:
                    print("UI did NOT register the file. We might need to dispatch an event or click something.")
            except Exception as e:
                print(f"Error uploading: {e}")
            
        print("Done.")
        await browser.close()

asyncio.run(run())
