https://github.com/alexchalva/tigernet_scraper 

Problem Decomposition:

The problem asked me to create a comprehensive scraper for TigerNet, Princeton University’s alumni database. The first step that I took was to visit TigerNet and manually explore the webpage. I knew one important challenge that I would have to face is the log-in interface based on Princeton’s CAS authentication system, so, I thoroughly examined and documented the manual steps I had to take as a user to log into the webpage. After that I generally explored the page, the different tabs and subpages, and the way the alumni directory was structured. I also explored and documented individual alumni profiles, the way they were structured and catalogued the data fields that I would be able to extract and were visible. After exploring the webpage manually, doing general research on scrapers, watching youtube videos on general scraper structures and techniques, and coordinating an extended conversation with ChatGPT where I thoroughly described the task at hand, provided pictures of the target website, and listed the specific success criteria that were provided, I formulated the following plan, which breaks down the problem into 5 main sub-problems: 

Authentication:

From my initial screening of TigerNet and the log-in process, I extracted valuable information with regards to the CAS authentication system. I realized that it follows a multi-step authentication flow. Princeton’s CAS system redirects the user to the DUO MFA, a 2-factor-authentication system that requires the user to accept a login request from their mobile device. Only then does the webpage redirect the user back to TigerNet, thus I realized that a separate platform disconnected from TigerNet handles login, and only after successful authentication redirects the user back to the original target webpage. To automate the login chain, I realized that I would firstly need to figure out exactly what tokens or cookies the chain produces and how long they last. After CAS validates the user’s identity, something gets handed back to TigerNet. It could be a cookie, a JWT access token, or an OAuth authorization token that gets exchanged for a token. The answer to that determines exactly how the scraper would need to authenticate its API calls. The duration of the credentials matter because that determines how often my scraper would need to re-authenticate. Especially since my goal is to scrape the entire alumni database (which contains ~130k profiles), I need to make sure my scraper has a consistent and reliable method of re-authenticating and creating new sessions so it can continue the scraping process uninterrupted. Next, I would need to solve the Duo MFA step, since the technical specifications require complete automation. Finally, after I create the appropriate session through the CAS tokens, I would need to figure out exactly the format the credentials are stored in. Figuring out which cookies and access tokens would be required for API access is crucial, since otherwise the API calls that my scraper rests in would fail. I would also need to find an effective re-authentication system that can handle expired auth tokens during the scraping, so the scraper can continue working independently.




Platform & API discovery:

After figuring out authentication, I know that I needed to figure out exactly the structure of the platform. Is it a custom Princeton system or a third party platform? Is there a structured API behind the frontend or would I need to parse HTML to extract the required information. How is pagination handled, what’s the maximum page number, etc. These were all important pieces of information that I needed to figure out.


Data Completeness

One of my success criteria was to capture every possible data field present at every single alumni profile, and most importantly, to not hardcore the target fields, but design a robust system that can extract all available information. So firstly, I needed to figure out if all the fields are returned in a single API response, or are spread across multiple endpoints. Do some profiles have fields that others don’t? Are there nested structures with fields and sub-fields? Also, if the API returns dynamic or inconsistent outputs, I would need to figure out how to set up a scraper in a way that presents all data uniformly in the CSV file without errors and problematic fields.


Scale 

From my initial scanning of the webpage, I could see that there were over 130,000 alumni, which means I would have to make a large number of API calls. I needed to think about how to avoid getting rate-limited or IP-blocked, what to do if the script crashes mid-scrape, how long a full scrape will take, and can I resume where I left off. I also needed to figure out the ideal balance between speed and safety(with regards to the scraper being blocked by the server). 


Data Export 

Before I started I was aware that any API response I would get would not be CSV-ready. The exporter would need to handle any exported data field into a readable and consistent format, handle missing values without printing “null” or “NaN” string, discover all possible columns across all the scraped profiles, and produce a UTF-8 CSV that I can cleanly import into Google Sheets.
 






Approach Exploration

API vs HTML scraping

The first major decision that I had to make was how to actually extract the data from TigerNet. I could either parse the rendered HTML pages using a library like BeautifulSoup, or find a structured API behind the frontend that can provide me with the required information. When I opened Chrome DevTools, and after analyzing the network tab with Claude and ChatGPT, I noticed that every time I clicked on an alumni profile, the browser was making XHR requests to different endpoints, and getting back clear JSON responses, thus, I realized that TigerNet’s frontend makes clear requests to a REST API behind the scenes. So, I didn’t need to scrape HTML at all, which can be very messy especially for a complicated web application like TigerNet. I could go directly to the API and get structured data that I can extract to a CSV. The used API was Hivebrite’s frontend API, with multiple different endpoints that I talk about later. I decided that this was by far the best option.


Authentication Strategy + API Call Strategy

This decision happened in two stages. First, I had to choose how to handle Princeton’s CAS + duo login. My initial research revealed that DUO MFA injects a JavaScript inframe that handles the push notification flow, and “requests” cannot execute JavaScript, so my initial thought of using raw “requests.Session” with HTTP redirects was scrambled. I decided to use Playwright, since it is relatively light, and built for modern web applications with support for waiting on URL pages(which is exactly what the scraper needed to handle for TigerNet.)

The second stage was unexpected. My original plan was to use Playwright only for login, and then use Python’s requests library for the actual scraping. But, when I tried that, the API calls returned HTML error pages instead of JSON and the server was returning 500s errors. I realized that Cloudflare's bot protection was blocking “requests” even though I was using the same cookies that worked in the browser. Apparently, Cloudflare doesn't allow raw Python HTTP requests and blocks them, so I had to pivot. I kept the Playwright browser open and made API calls using page.evaluate(fetch(...)), using JavaScript fetch() calls from the authenticated browser context. That way, every request falls under Cloudflare’s clearance. It might be slower than raw HTTP requests, but it actually works without errors. 


Data Extraction

Initially, I only knew about two API endpoints from my DevTools research for the listing endpoint and the paginated directory data, and the full profile endpoint for individual users. After building the first scraper around these two, I checked the exported CSV file and realized that many of the data fields visible in alumni profiles on TigerNet were not present on the CSV. I went back to Chrome DevTools, and analyzed them again using Claude and ChatGPT. That’s when Claude told me that the frontend was making a second API call that I had not seen: /users/{id}/users/{id}/data. This endpoint returns a different JSON, with the data organized in named sections, each containing an array of field objects with display names and values. Since the goal of the assignment was to extract every data field present in alumni profiles, I added a second API call per user in the scraper that retrieves all available information from this API call. This allowed me to meet the requirement of extracting every data field, although it made my scraper significantly slower, since another API call per user was added. Unfortunately, this was the only way to extract all information, which is approximately 120 different data fields per user, so it makes sense that it increases the required time. I also had to rewrite the parser in the exporter that dynamically extracts every field.



Parallelism strategy

After creating the first working prototype of the scraper, I realized that it was just too slow, for the reason that I mentioned before. Theoretically, the scraper would need to extract information for 130,000 alumni, so with two API calls per user, the time really adds up. I had the idea of implementing some kind of parallel structure that would increase efficiency and speed, and after asking Claude and ChatGPT, they both proposed I open multiple browser tabs in the same Playwright context so they can all share the same sessions cookies, and use Python’s threading module to run them in parallel. I implemented a shared user queue that each thread would pull users from, and make API calls. Unfortunately, even though both LLMs said it would be a good idea, it did not work at all. Every single API call threw “Cannot switch to a different thread” error so I decided to give up on that idea. But, after more conversation with Claude and research, I learned that “Playwright’s sync API is built on top of greenlets internally, and greenlets are cooperative coroutines that are bound to the specific thread that created them” - Claude. Thus, my original idea was just not executable. 

After more conversation, I found the solution, which was to switch to Python’s asyncio, with Playwright’s async API. I used asyncio.gather() to run multiple worker coroutines concurrently on a single thread, which doesn’t violate the previous technical limitations. I was able to run multiple tabs indeed through asyncio. To figure out the number of tabs, I manually ran tests where I was changing the number of open tabs and measuring the time required to scrape a set number of users, and with 4 open tabs, the scraper is almost 4 times faster than originally. I did not increase the number of tabs past 4 since pushing higher risks triggering rate limiting, especially if I start scraping a larger number of users.






Tradeoffs 
Speed vs. Safety
The most tangible tradeoff I dealt with throughout the project was tuning the request delay — the pause between API calls that prevents the server from rate-limiting or blocking my IP. I started with a conservative 1.5-second delay, which is a common safe default for scrapers. But at that rate, scraping 130K profiles would take over 54 hours. That's not practical.
So I started testing empirically. I reduced the delay to 0.5 seconds and ran a batch of 100 profiles, then I tried 0.2 seconds. I considered going even lower to 0.1 seconds, but decided that 0.2 was the right stopping point. The API response time itself is around 200-300 milliseconds, so a 0.2-second delay means I'm roughly doubling the natural request interval. Going below that would mean I'm actively trying to send requests faster than the server can respond, which is aggressive behavior that could easily trigger rate limiting during a 130K-profile scrape. Combined with 4 parallel tabs, the final full scrape takes roughly 35 hours, which is acceptable.
API Calls
Using three API endpoints per user: listing, full profile, and profile data means roughly 262,000 individual API calls for the full directory, plus about 1,300 listing pages. I could have stopped at just the listing and full profile endpoints, which would have cut the total API calls nearly in half and saved several hours of scraping time. But I would have lost student activities, volunteer work, nickname, and about 20 other Princeton-specific fields that only come from the /data endpoint. Since the assessment asks for "every available field," and student activities are arguably one of the most interesting fields in an alumni directory, I decided the extra time was worth it. The /data endpoint adds roughly 10 seconds per 100-user batch with parallelism, extending the total scrape by about 3-4 hours.

Progress Checkpointing Granularity
I chose to save progress every 50 profiles. This was a deliberate balance between two concerns. Saving more frequently would minimize data loss if the scraper crashes, but it adds I/O overhead since I'm writing a JSON file containing up to 130K profile IDs to disk each time. Saving less frequently, every 500 profiles, but if the scraper crashes, then it needs to rescrape for about 5 minutes to make up for the lost information. At every 50 profiles, the worst-case loss is about 30 seconds of scraping, which is negligible given that the full run takes 30+ hours.





Obstacles


Cookie consent banner

My initial scraper solution couldn’t even log in and reach Duo, since it wouldn’t be able to reach and “click” the login button.  When I looked at the screenshot of what the headless browser was actually rendering, I saw a full-page cookie consent banner was sitting on top of everything, intercepting all click events. The Login button was there underneath, but the banner was blocking it completely.

I added some extra code that runs before everything else, that clicks “accept all cookies” and dismisses the banner. That fixed the issue and the scraper was able to move to the login step.


Extracting the user ID after login
The full profile API endpoint has an unusual URL pattern: /users  /{my_id} /users /{target_id} ?full_profile=true.  It requires not just the target user's ID, but also the authenticated user's own Hivebrite ID. I needed to extract this automatically after login. My first approach was to decode the JWT access token from the api_access_token cookie, since the JWT payload contains the user ID in its ext.user_id field. But this didn't work reliably, since sometimes the cookie wasn't set yet when my code tried to read it. The Hivebrite SPA initializes asynchronously after the page loads, and certain cookies get set during that initialization process rather than immediately on page load. So sometimes the JWT was there and I could decode it, and other times my code ran too early and got nothing.
I fixed this by adding a 5-second wait after login to let the SPA fully initialize, and I built a chain of fallback methods for extracting the user ID: first try decoding the JWT, then try making an API call to an endpoint that returns user metadata, then try navigating to the profile page and extracting the ID from the URL. In practice, the JWT decode works every time after the wait period, but having the fallbacks means the scraper won't break if Hivebrite changes how or when they set cookies.

4. URL encoding breaking API calls
Right after switching to browser-context API calls, the directory listing endpoint started returning 500 errors on every request. The exact same URLs that had been working with requests (back when it was returning HTML instead of JSON) were now failing with fetch() too. I spent time checking cookies, headers, and authentication state before finally comparing my URL string character-by-character against the working URLs in DevTools. The issue was URL encoding. I was using percent-encoded brackets — query%5Bexclude_current_user%5D=false — which is technically correct and which requests handles fine. But the browser's fetch() API was double-encoding them, turning %5B into %255B, which the server didn't understand. Switching to raw brackets — query[exclude_current_user]=false fixed it immediately because the browser handles any necessary encoding internally. A small issue, but it took real debugging time because the 500 error gave no indication of what was actually wrong with the request.
5. Missing student activities and volunteer data
After getting full profiles working and producing what I thought was a complete CSV, I opened it in Excel and started comparing the columns against what I could see on actual TigerNet profile pages in the browser. That's when I noticed the gaps. Student Activities, Volunteer Activities, Nickname, Primary Affiliation, Regions, Affinity Groups, all of these were clearly visible on the profile page in the browser, but they weren't in my CSV at all. The full_profile API endpoint simply didn't return them.
I went back to Chrome DevTools and loaded a profile page while watching the Network tab very carefully, paying attention to every single request the frontend made. That's when I spotted a second API call I'd previously missed: /users/{id}/users/{id}/data. This endpoint returns an entirely different JSON structure from the full profile endpoint — instead of a flat user object, it returns data organized into named sections like "Princeton Information," "Profile Information," and "Alumni Service (Current)," each containing arrays of field objects with display_name and value keys. I captured the raw response, studied the nesting structure, and built a new parser that walks through all the sections and pulls out every field with a non-null value.

6. Parallelism
As I mentioned in the previous section, implementing parallelism was a major challenge, but with the use of asyncio API, I was able to make it work and increase speed and efficiency.

7. Token Expiration
After getting the scraper working reliably for batches of 200-300 profiles, I realized I had a much bigger problem for the full 130K scrape. The JWT access token expires after about one hour, but a complete scrape takes over 20 hours. That means the tokens would expire roughly 20 times during a full run. When that happens, every API call starts returning errors, all the workers stop, and the scrape is dead.
My first instinct was to just tell the user to re-run with --resume after each expiration — the progress file tracks which profiles have been fetched, so the scraper can skip already-completed users and pick up where it left off. But that means someone has to babysit the process for 20+ hours, manually re-authenticating every hour. That's not real automation.
So I built an auto re-auth loop around the scraping process. The workers track consecutive API failures — if 10 requests in a row fail (which strongly indicates token expiration rather than a one-off network glitch), all workers stop and save their progress. Then the outer loop detects that there are still profiles remaining, calls refresh_tokens() to open a fresh browser and re-authenticate through CAS + Duo, gets new tokens, and kicks off a new batch of workers that resume from exactly where the last batch stopped. This cycle can repeat up to 5 times.
The one limitation I couldn't fully solve is the Duo MFA step. Every re-authentication requires a human to approve the Duo push notification on their phone. I implemented persistent browser profiles using Playwright's launch_persistent_context so that Duo's "remember this device" cookie survives between sessions — in theory, after the first Duo approval, subsequent re-authentications should skip MFA entirely. But this depends on Princeton's Duo configuration and how long they honor the "remember" cookie. In the worst case, the user gets a Duo push notification once per hour during the scrape, which is far better than having to manually restart the entire process.





















AI Collaboration 

The main AI tools I used for this project are Claude, ClaudeCode, and ChatGPT. For development, Claude and ClaudeCode were exclusively used. After accessing TigerNet, and simulating the function of a scraper by going to the correct directory and clicking through multiple profiles, I took screenshots of the login flow, directory page, individual, and most importantly, the Chrome DevTools Network tab showing all API requests. I fed these screenshots directly to Claude, together with detailed instructions on what I wanted to build, including some copied parts from the assignment itself, and asked it to analyze the network requests, since I knew this was the main way to extract information on how the website works in its core. It was able to figure out that the platform was running on Hivebrite, and it also identified the JWT token structure and figured out how to decode it to extract the user ID. This saved me significant time. I could have identified these things by myself by reading the cookies and manually decoding the JWT, but AI did it in seconds from just the screenshots I provided.

For the code, Claude produced the initial project skeleton. Of course, this generated code did not work, and it took numerous rounds of feedback and debugging to achieve the correct function, but, having a structured starting point with proper separation of concerns into different Python scripts that interact with each other made the project a lot more manageable. 

For debugging, I started by running the initial code Claude outputted. I would then inspect the errors that would arise and thoughtfully document them back into Claude. I would also provide specific screenshots of the TigerNet that were relevant to the errors I was facing at any time, and also, I always included a paragraph that described my own interpretation of the problem and what I thought the reason for it was. I employed a constant feedback loop with Claude and ClaudeCode, where the errors generated were fed back into the AI, with my comments and guidance to ensure the fixes were targeted. AI accelerated the Code generation process by a tremendous amount. Even though I was not familiar with a lot of the tools required to create such a scraper, I didn’t have to spend countless hours reading documentation and figuring out how each library is used. 

A prime example is the Cloudflare issue. When requests kept returning HTML instead of JSON, I shared the full error output with Claude, together with detailed information from the Network tab. Claude identified Cloudflare as the likely blocker and suggested the page.evaluate(fetch()) approach, which is something that would have taken me a tremendous amount of time to figure out. 

There were multiple instances where the fixes AI suggested were not very correct, and that was part of the process. For example, Claude initially suggested that Python’s request library would work for API calls after Playwright authentication, which was not true, and as a result, a lot of time was spent trying to make requests work where it was just not possible, and at the end, I had to change the whole scraper.py implementation. As the engineer, I was the one that suggested to Claude to explore different methods through detailed prompting, and as a result, the final correct approach was discovered and employed.

Then, after I suggested we implement some kind of parallelism to increase efficiency and time, Claude suggested using Python’s threading module, which is fundamentally incompatible with Playwright’s sync API (as I realized later.) Again, a reasonable suggestion on paper to implement parallelism, but it doesn’t work with Playwright’s architecture. I spent a lot of time analyzing the error messages, feeding them back into the AI, and iterating on feedback, before we (Claude and I) finally realized that the approach itself was wrong, not just the implementation. 

These were just some of the times AI led me astray, but regardless, these wrong implementations and ideas was what allowed me to complete the project. I used AI in a way that it was able to correct its own mistakes, and with constant and directed feedback and my own ideas, every time I was led astray by it I was brought back to the correct course more efficiently and faster than I would have on my own.

Finally, something that I did throughout the process of this project was cross checking information between two LLMs - Claude and ChatGPT. Especially in the beginning, when I was analyzing the network tabs and making crucial decisions that would determine the approach I would take to create the scraper, especially after the first major mistake that I made which was to try to implement the scraper with raw HTTP requests with Python's request library, any time that I felt Claude was making a “big” decision, I would always try to reach that same decision in ChatGPT. Since I used a lot of new tools that I have never used before, and have had very limited experience with making scrapers, I realized that a much faster and efficient way to make sure the steps I am taking are correct is to follow this method. I understand that it is not 100% foolproof, but considering the advancements in LLMs, I made the decision that if Claude and ChatGPT give me the same proposals with regards to what methods/tools I should use, or if ChatGPT verifies code/output that Claude gave me, I would move forward with that implementation. That saved me a lot of time and prevented mistakes like the one I mentioned in the beginning of the paragraph to happen again.

