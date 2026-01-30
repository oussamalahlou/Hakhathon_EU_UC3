const express = require('express');
const axios = require('axios');
const path = require('path');

const app = express();
const LAMBDA_URL = 'https://skjx9klvgi.execute-api.us-east-1.amazonaws.com/processRequest';

app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true }));
// Serve static files from workspace root so you can open http://localhost:3000
app.use(express.static(path.join(__dirname)));

// Proxy endpoint: receives JSON from the browser and returns a mock response for testing
app.post('/api/lambda', async (req, res) => {
  try {
    console.log('Lambda proxy endpoint received:', req.body);

    // Allow optional delay for testing (ms).
    // Default to 0ms (no delay); override by calling `/api/lambda?delay=3000`.
    const delayMs = Number(req.query.delay) || 0;

    // Handler function to call the real Lambda
    const callLambda = async () => {
      try{
        console.log('Calling Lambda at:', LAMBDA_URL);
        // Pass the entire request body to Lambda (it expects { text: "..." })
        const lambdaResp = await axios.post(LAMBDA_URL, req.body, {
          headers: { 'Content-Type': 'application/json' },
          timeout: 90000
        });
        console.log('Lambda response:', lambdaResp.data);
        res.status(200).json(lambdaResp.data);
      }catch(lambdaErr){
        console.error('Lambda call failed:', lambdaErr.message);
        res.status(lambdaErr.response?.status || 500).json({
          error: 'lambda_error',
          message: lambdaErr.message,
          details: lambdaErr.response?.data || null
        });
      }
    };

    // Apply delay if specified, then call Lambda
    if(delayMs > 0){
      console.log(`Delaying Lambda call by ${delayMs}ms`);
      setTimeout(callLambda, delayMs);
    } else {
      callLambda();
    }
  } catch (err) {
    console.error('Error:', err.message);
    res.status(500).json({ error: 'server_error', message: err.message });
  }
});

// Endpoint to receive client approval (mock)
app.post('/api/approve', express.json(), async (req, res) => {
  try{
    console.log('Approval endpoint received:', req.body);
    // In a real implementation we'd persist approval and trigger an email/send contract flow.
    const { requestId, approve } = req.body || {};
    if(approve){
      return res.status(200).json({ status: 'ok', message: 'Approval recorded; contract will be sent by email.', requestId });
    } else {
      return res.status(200).json({ status: 'ok', message: 'User declined automatic processing.', requestId });
    }
  }catch(err){
    console.error('Approve error', err);
    res.status(500).json({ error: 'server_error', message: err.message });
  }
});

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Server running at http://localhost:${port}`));
