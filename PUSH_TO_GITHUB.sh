#!/bin/bash
# Quick script to push PR branch and get GitHub PR URL

echo "🚀 Pushing GCR Slider land crossing fix to GitHub..."
echo ""

# Push the branch
git push origin fix/gcrslider-land-crossing

echo ""
echo "✅ Branch pushed successfully!"
echo ""
echo "📝 Next steps:"
echo "   1. Go to: https://github.com/52North/WeatherRoutingTool/compare"
echo "   2. Click 'compare across forks'"
echo "   3. Select: base: 52North/WeatherRoutingTool main"
echo "   4. Select: head: YourFork/WeatherRoutingTool fix/gcrslider-land-crossing"
echo "   5. Title: 'Fix GCR Slider land crossing detection'"
echo "   6. Body: Copy from PR_SUMMARY.md"
echo ""
echo "📋 Files to reference in PR:"
echo "   - PR_SUMMARY.md (detailed description)"
echo "   - NAVICA_ENHANCEMENTS.md (technical details + future work)"
echo ""
echo "Ready to create PR! 🎉"
