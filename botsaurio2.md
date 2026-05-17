# FROM CHAOS TO INTENT
### The real, unvarnished human journey of building a personal job engine

## The Manifesto: Survival Over Metrics
This project didn’t start as a calculated addition to a polished resume, nor was it built to collect superficial stars on GitHub. It was born out of raw, unadulterated frustration—the deep mental exhaustion that comes with navigating the modern tech job market. Anyone who has looked for a role recently knows the depressing loop: endless page refreshes, fighting low-fidelity algorithms, and watching ghost job counts spike. **Automation wasn't a clever technical side-hobby; it was a psychological survival mechanism to keep from burning out.**

This log isn't here to boast about framework benchmarks. It is the story of how an unstable 20-line notification script evolved through brutal physical constraints, a massive accidental monolith, and an unconventional mobile development sandbox, completely changing how I view software development along the way.

---

## The Evolution: Fighting the Constraints

### 01 // THE PRIMITIVE SEED
#### Code Sharing & Late-Night Windows
The first working version was basic: raw manipulation of URL query parameters to replicate my manual browser filtering. I didn't even have a dedicated computer to run or write the code at the time. I was entirely dependent on borrowing my brother's laptop whenever he finished working on his university graduation thesis late at night. If he was using the machine, the job search engine died. Because of that, early alerts were laggy, teaching me the hard way that in high-volume job markets, being an hour late means your application disappears into a black hole.

### 02 // THE CLOUD DETOUR
#### Telegram Infrastructure & Free Tier Battles
To stop missing alerts and free myself from the shared laptop, I needed a 24/7 environment. I immediately threw out heavy, high-level frameworks and corporate chat APIs, writing raw wrappers for the Telegram Bot API instead. Why? Instantaneous delivery directly to my pocket. Moving to the cloud was another headache—commercial trials kept bouncing my cards, forcing me to map out a bare-minimum setup on Oracle Cloud's (OCI) Always-Free tier, running on a tight 1GB memory cap. The bot was finally awake around the clock.

### 03 // GUERRILLA ENGINEERING
#### The Termux Android Sandbox
Just when things seemed stable, academic pressures meant the shared laptop became completely unavailable. With zero budget for a new machine, I rescued an old, cracked Android phone from a drawer. It had 6GB of RAM, which was hilariously more powerful than my free cloud instance. I deployed Termux and used Acode to edit files directly on the touchscreen. Coding a concurrent architecture with an on-screen keyboard was a masterclass in patience. Fighting broken binary dependencies and Android's storage restrictions forced me to master low-level Linux filesystems and toolchains just to make Python behave.

### 04 // THE MONOLITH EXPLOSION
#### 3,000 Lines & The Behavioral War
Having a full Linux playground directly on my phone turned into an obsession. I was injecting features for 12 hours a day without an architectural roadmap. Before I knew it, the project exploded into an indomitable, single-file functional monolith of nearly 3,000 lines. At this stage, the project shifted from data extraction to platform behavioral warfare. To stop getting blocked, I spent weeks analyzing anti-scraping patterns, implementing mathematical execution jitter, shifting request windows, and building cooling cycles so the code emulated a careful, organic human operator.

### 05 // THE PIVOT
#### Debian Containers & Human Intent
The monolith eventually broke. A single bug fix would trigger unpredictable regressions across hidden side-effects, and upgrading to modern tools like Pydantic V2 completely shattered the native Termux python environment due to binary compilation bugs. To fix it, I nested a clean Debian environment inside a Termux chroot, isolating my development layers. I systematically broke the monolith down with a scalpel, separating responsibilities into clean domain modules. That was the moment of clarity: the real milestone wasn't building a faster scraper. Scraping was just a crude extraction layer. The real value was bridging the massive semantic gap between human intent and rigid search query engines.

---

## The Core Philosophy Behind bot.py
Because this project survived in high-friction environments, the architecture of the main entry point is fiercely minimal. It completely avoids framework abstraction bloat—using raw HTTP wrappers to control network layers, using lightweight state machines for user configurations, and isolating search passes inside threads controlled by atomic events. It’s built to be robust, resource-light, and entirely self-owned.

## The Takeaway
Severe infrastructure and hardware constraints are the best software engineering teachers you can ask for. Lacking a high-spec development setup forced me to write efficient logic, respect memory footprints, and ruthlessly cut out unnecessary third-party packages. In the end, I didn't just build a tool to escape the exhausting loop of job hunting—I changed my entire perspective on how to build software under pressure.

---

*Bucaramanga, Santander, Colombia // 2026*

*"Never should have come here 🐲"*

_"Este proyecto quizá nació de la frustración pero más allá de eso me llevo a comprender que lo que más ansiaba era aceptación, no de nadie más, sino de mi mismo y mis habilidades, espero que al concluirlo, sirva para que nadie más se sienta como me sentí yo."_
