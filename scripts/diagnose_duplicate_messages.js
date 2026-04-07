// 在浏览器控制台（F12）运行这段代码，检查消息重复情况

// 1. 检查 DOM 中的消息数量
const messagesDOM = document.querySelectorAll('.message');
console.log('DOM 中的消息数量:', messagesDOM.length);

// 2. 检查消息 ID 是否有重复
const messageIds = Array.from(messagesDOM).map(el => el.dataset.id);
const uniqueIds = new Set(messageIds);
console.log('消息 ID 数量:', messageIds.length);
console.log('唯一 ID 数量:', uniqueIds.size);
console.log('是否有重复:', messageIds.length !== uniqueIds.size);

// 3. 如果有重复，找出重复的消息
if (messageIds.length !== uniqueIds.size) {
    const duplicates = messageIds.filter((id, index) => messageIds.indexOf(id) !== index);
    console.log('重复的消息 ID:', duplicates);

    // 显示重复消息的内容
    duplicates.forEach(id => {
        const elements = document.querySelectorAll(`[data-id="${id}"]`);
        elements.forEach(el => {
            console.log('重复消息内容:', el.textContent.substring(0, 50));
        });
    });
}

// 4. 检查 oldestMessageId
console.log('oldestMessageId:', typeof oldestMessageId !== 'undefined' ? oldestMessageId : 'undefined');

// 5. 检查浏览器 localStorage 是否有缓存
const cachedMessages = localStorage.getItem('chat_messages');
console.log('localStorage 中是否有缓存:', cachedMessages ? '是' : '否');
