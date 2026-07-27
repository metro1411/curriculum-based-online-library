# Start Here - local setup in four clicks

1. Extract the ZIP completely. Do not run it from inside the ZIP.
2. Open the extracted folder and double-click `START_HERE.bat`.
3. Choose **1** to create and edit `.env`. This file must be named exactly `.env`, not `.env.txt`.
4. Choose **2** to run the automated check. After it passes, choose **3** to start the website.

The local address is `http://127.0.0.1:5055`.

## What to put in `.env`

```env
GEMINI_API_KEY=
GEMINI_ENABLE_WEB_GROUNDING=0
HOD_ACTIVATION_CODE=your-private-code-with-at-least-16-characters
```

`GEMINI_API_KEY` can stay empty until you have a real key. The site still starts; generated Gemini answers are simply unavailable until a valid key is added. The HOD activation code is needed only when creating the first `5000...` Head of Department account.

To publish to Render after local testing, open `DEPLOY_RENDER.md` from the same menu. Never upload `.env` to GitHub.
