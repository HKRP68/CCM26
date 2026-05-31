const fetch = require('node-fetch');

/**
 * Utility to talk to Adsgram API
 */
class Adsgram {
    constructor(blockId, token) {
        this.blockId = blockId;
        this.token = token;
    }

    /**
     * Fetches an ad for a specific user
     */
    async getAd(tgUserId, language = 'en') {
        // If no credentials, return a MOCK ad for demonstration
        if (!this.blockId || !this.token) {
            return this.getMockAd();
        }

        try {
            const url = `https://api.adsgram.ai/advbot?tgid=${tgUserId}&blockid=${this.blockId}&language=${language}&token=${this.token}&debug=true`;
            console.log("Calling Adsgram URL:", url);
            const response = await fetch(url);
            if (!response.ok) {
                const errorBody = await response.text();
                console.error(`Adsgram API Error: ${response.status} - ${errorBody}`);
                throw new Error(`Adsgram API Error: ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.error("Failed to fetch ad from Adsgram:", error.message);
            return null;
        }
    }

    /**
     * Mock data to show the user how it looks without real IDs
     */
    getMockAd() {
        return {
            image_url: "https://picsum.photos/seed/adsgram/800/450", 
            text_html: "🔥 <b>Discover Draconic Legends!</b>\n\nBattle dragons, join guilds, and win massive rewards in the newest RPG on Telegram!\n\n<i>This is a sample ad message.</i>",
            button_name: "🎮 Play Now",
            click_url: "https://t.me/example_bot",
            button_reward_name: "💰 Claim 500 Coins",
            reward_url: "https://example.com/reward" 
        };
    }
}

module.exports = Adsgram;
